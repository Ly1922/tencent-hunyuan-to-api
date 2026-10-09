import asyncio
import base64
import contextlib
import io
import json
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
import httpx
with contextlib.redirect_stdout(io.StringIO()):
    from app.hunyuan_client import HunyuanAccount, HunyuanAccountPool, GeneratedImageDownloadError
    from app.config import settings

IMAGE_URL = 'https://cdn.fixture/image.png'
IMAGE_DATA = b'fixture-generated-image'

def sse():
    return 'data: ' + json.dumps({'type':'image','imageUrlHigh':IMAGE_URL,'width':2048,'height':2048}) + '\n\n'

class Optimizations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.accounts = []
    async def asyncTearDown(self):
        await asyncio.gather(*(account.aclose() for account in self.accounts))
    def account(self, name='fixture'):
        account = HunyuanAccount(name, 'fixture-cookie', 'fixture-chat')
        self.accounts.append(account)
        return account
    def transport(self, account, handler):
        for redirects in (False, True):
            account._clients[redirects] = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=redirects)

    async def test_upload_failure_releases_account_and_counts_failure(self):
        account = self.account()
        account.upload_image = AsyncMock(side_effect=RuntimeError('fixture upload failure'))
        with self.assertRaisesRegex(RuntimeError, 'fixture upload failure'):
            await account.execute_generate('fixture', image_input=b'fixture')
        self.assertFalse(account.is_busy)
        self.assertFalse(account.lock.locked())
        self.assertEqual(account.failed_count, 1)

    async def test_cancellation_releases_account_and_upload_tasks(self):
        account = self.account()
        started = asyncio.Event()
        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()
        account._upload_image_uncached = blocked
        task = asyncio.create_task(account.execute_generate('fixture', image_input=b'fixture'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(account.is_busy)
        self.assertFalse(account.lock.locked())
        self.assertFalse(account._reference_inflight)

    async def test_cache_deduplicates_content_and_keeps_role_names(self):
        account = self.account()
        async def uploaded(data, filename):
            await asyncio.sleep(0.01)
            return {'url':'https://cdn.fixture/reference','fileName':filename,'name':filename}
        account._upload_image_uncached = AsyncMock(side_effect=uploaded)
        first, second = await asyncio.gather(account.upload_image(b'same','main.png'), account.upload_image(b'same','reference.png'))
        self.assertEqual(account._upload_image_uncached.await_count, 1)
        self.assertEqual(first['fileName'], 'main.png')
        self.assertEqual(second['fileName'], 'reference.png')
        await account.upload_image(b'same','again.png')
        self.assertEqual(account._upload_image_uncached.await_count, 1)
        another = self.account('second-account')
        another._upload_image_uncached = AsyncMock(return_value={'url':'https://cdn.fixture/another-account'})
        self.assertEqual((await another.upload_image(b'same'))['url'], 'https://cdn.fixture/another-account')
        self.assertEqual(another._upload_image_uncached.await_count, 1)

    async def test_cache_expiry_and_bounded_lru(self):
        account = self.account()
        account._upload_image_uncached = AsyncMock(return_value={'url':'https://cdn.fixture/reference'})
        with patch.object(settings, 'REFERENCE_CACHE_TTL', 0.01):
            await account.upload_image(b'expiring')
            await asyncio.sleep(0.02)
            await account.upload_image(b'expiring')
        self.assertEqual(account._upload_image_uncached.await_count, 2)
        account._reference_cache.clear()
        account._upload_image_uncached.reset_mock()
        with patch.object(settings, 'REFERENCE_CACHE_MAX_ITEMS', 2):
            for content in (b'a',b'b',b'a',b'c',b'b'):
                await account.upload_image(content)
            self.assertLessEqual(len(account._reference_cache), 2)
        self.assertEqual(account._upload_image_uncached.await_count, 4)
        expiry = account._cache_expiry('https://cdn.fixture/ref?q-sign-time=0;%d' % (time.time()+30))
        self.assertLessEqual(expiry-time.monotonic(), 25.1)

    async def test_parallel_uploads_are_bounded_and_order_is_preserved(self):
        account = self.account()
        active = 0
        maximum = 0
        submitted = []
        async def upload(data, filename):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.02 if data == b'first' else 0.005)
                return {'url':'https://cdn.fixture/'+data.decode()}
            finally:
                active -= 1
        account._upload_image_uncached = upload
        def respond(request):
            if request.method == 'POST':
                submitted.extend(json.loads(request.content)['multimedia'])
                self.assertEqual(request.extensions['timeout']['read'], settings.REQUEST_TIMEOUT)
                return httpx.Response(200, text=sse())
            return httpx.Response(200, content=IMAGE_DATA)
        self.transport(account, respond)
        result = await account.execute_generate('fixture', image_input=[b'first',b'second',b'third'], response_format='b64_json')
        self.assertEqual([item['url'] for item in submitted], ['https://cdn.fixture/first','https://cdn.fixture/second','https://cdn.fixture/third'])
        self.assertEqual(maximum, min(3, settings.UPLOAD_CONCURRENCY))
        self.assertEqual(base64.b64decode(result['b64_json']), IMAGE_DATA)
        self.assertEqual(result['diagnostics']['reference_uploads'], 3)
        self.assertIn('total', result['diagnostics']['timings_seconds'])

    async def test_http_client_is_reused_and_closed(self):
        account = self.account()
        async with account._get_client(timeout=30) as first:
            pass
        self.assertFalse(first.is_closed)
        async with account._get_client(timeout=120) as second:
            self.assertIs(first, second)
        await account.aclose()
        self.assertTrue(first.is_closed)

    async def test_cos_upload_does_not_block_the_event_loop(self):
        account = self.account()
        worker_threads = []
        def respond(request):
            return httpx.Response(200,json={'bucketName':'fixture-bucket','location':'fixture-key','region':'fixture-region','encryptTmpSecretId':'fixture','encryptTmpSecretKey':'fixture','encryptToken':'fixture'})
        self.transport(account, respond)
        class FakeCos:
            def put_object(self, **kwargs):
                worker_threads.append(threading.get_ident())
                time.sleep(0.1)
            def get_presigned_download_url(self, **kwargs):
                return 'https://cdn.fixture/reference'
        ticks = 0
        with patch('app.hunyuan_client.CosConfig'), patch('app.hunyuan_client.CosS3Client', return_value=FakeCos()):
            upload = asyncio.create_task(account.upload_image(b'fixture'))
            while not upload.done():
                ticks += 1
                await asyncio.sleep(0.01)
            await upload
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        self.assertGreater(ticks, 2)

    async def test_generated_download_retries_without_another_generation(self):
        account = self.account()
        counts = {'post':0,'get':0}
        def respond(request):
            key = request.method.lower()
            counts[key] += 1
            if key == 'post': return httpx.Response(200,text=sse())
            return httpx.Response(503) if counts['get'] == 1 else httpx.Response(200,content=IMAGE_DATA)
        self.transport(account, respond)
        result = await account.execute_generate('fixture',response_format='b64_json')
        self.assertEqual(base64.b64decode(result['b64_json']), IMAGE_DATA)
        self.assertEqual(counts, {'post':1,'get':2})

    async def test_permanent_download_failure_does_not_failover_and_regenerate(self):
        account = self.account()
        def respond(request):
            return httpx.Response(200,text=sse()) if request.method == 'POST' else httpx.Response(503)
        self.transport(account, respond)
        pool = HunyuanAccountPool.__new__(HunyuanAccountPool)
        pool.accounts = [account,self.account('second')]
        pool.get_account = AsyncMock(return_value=account)
        with self.assertRaises(GeneratedImageDownloadError):
            await pool.generate_image('fixture',response_format='b64_json')
        self.assertEqual(pool.get_account.await_count, 1)
        self.assertFalse(account.is_busy)

    async def test_cache_can_be_disabled(self):
        account = self.account()
        account._upload_image_uncached = AsyncMock(return_value={'url':'https://fixture/image'})
        for field in ('REFERENCE_CACHE_TTL', 'REFERENCE_CACHE_MAX_ITEMS'):
            with patch.object(settings, field, 0):
                await account.upload_image(b'fixture')
                await account.upload_image(b'fixture')
                self.assertEqual(len(account._reference_cache), 0)
        self.assertEqual(account._upload_image_uncached.await_count, 4)

    async def test_wrapped_base64_reference_remains_compatible(self):
        account = self.account()
        account.upload_image = AsyncMock(return_value={'url':'https://fixture/image'})
        encoded = base64.b64encode(IMAGE_DATA).decode()
        wrapped = 'data:image/png;base64,' + encoded[:4] + '\n' + encoded[4:] + ' '
        await account._prepare_reference(wrapped, 0)
        account.upload_image.assert_awaited_once_with(IMAGE_DATA, 'reference_1.png')

    async def test_reload_preserves_unchanged_clients_and_drains_retired_account(self):
        records = [{'name':'fixture','cookie':'fixture','chat_id':'fixture'}]
        with patch.object(type(settings),'load_accounts',return_value=records):
            pool = HunyuanAccountPool()
            original = pool.accounts[0]
            self.accounts.append(original)
            async with original._get_client() as client:
                pass
            pool.reload_accounts()
            self.assertIs(pool.accounts[0],original)
            await original.lock.acquire()
            with patch.object(type(settings),'load_accounts',return_value=[]):
                pool.reload_accounts()
            await asyncio.sleep(0)
            self.assertFalse(client.is_closed)
            original.lock.release()
            await asyncio.gather(*list(pool._cleanup_tasks))
            self.assertTrue(client.is_closed)
            await pool.aclose()

    async def test_openai_route_keeps_base64_and_diagnostics(self):
        from app.main import app
        fake = {'b64_json':base64.b64encode(IMAGE_DATA).decode(),'diagnostics':{'timings_seconds':{'total':1.2},'reference_cache_hits':1}}
        with patch('app.routes.hunyuan_pool.generate_image',new=AsyncMock(return_value=fake)) as generate, patch.object(settings,'API_KEYS',''):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                response = await client.post('/v1/images/generations',json={'prompt':'fixture','images':['first','second'],'response_format':'b64_json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data'][0]['b64_json'],fake['b64_json'])
        self.assertEqual(response.json()['diagnostics'],fake['diagnostics'])
        self.assertEqual(generate.await_args.kwargs['image_input'], ['first','second'])

if __name__ == '__main__':
    unittest.main()
