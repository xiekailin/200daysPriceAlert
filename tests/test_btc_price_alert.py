"""btc_price_alert 回归测试（仅用标准库 unittest，网络请求全部打桩）。

运行：python3 -m unittest discover -s tests -v
"""
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(workdir):
    """在临时目录里导入脚本：模块导入时会写日志、读状态文件，都使用相对路径。"""
    if 'requests' not in sys.modules:
        sys.modules['requests'] = types.ModuleType('requests')
    os.chdir(workdir)
    sys.path.insert(0, PROJECT_DIR)
    try:
        sys.modules.pop('btc_price_alert', None)
        return importlib.import_module('btc_price_alert')
    finally:
        sys.path.remove(PROJECT_DIR)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.m = load_module(self._tmp.name)
        self.m.print = lambda *a, **k: None  # 测试时不刷屏

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def manager(self, cooldown=30, band=0.2):
        return self.m.SmartAlertManager(cooldown_minutes=cooldown, hysteresis_pct=band,
                                        state_file='state_test.json')


class CooldownTest(BaseCase):
    def test_multi_level_cross_keeps_all_cooldowns(self):
        """一次穿越多个关口时，每个关口的冷却都要保留，不能互相覆盖。"""
        mgr = self.manager()
        now = 1_000_000
        self.assertEqual(mgr.check_level('L61000', 60500, 62500, 61000, now), '涨破')
        self.assertEqual(mgr.check_level('L62000', 60500, 62500, 62000, now), '涨破')
        self.assertEqual(set(mgr.cooldown), {'L61000', 'L62000'})
        # 冷却期内回落穿越 61000，不应再推送
        self.assertIsNone(mgr.check_level('L61000', 62500, 60500, 61000, now + 300))

    def test_cooldown_expires(self):
        mgr = self.manager(cooldown=30)
        now = 1_000_000
        mgr.check_level('L', 60500, 61500, 61000, now)
        self.assertEqual(mgr.check_level('L', 61500, 60500, 61000, now + 31 * 60), '跌破')

    def test_state_persisted_and_old_format_migrated(self):
        with open('state_test.json', 'w') as f:
            json.dump({'L1': {'time': 123.0, 'state': 'above'}, 'L2': 456}, f)
        mgr = self.manager()
        self.assertEqual(mgr.cooldown, {'L1': 123.0, 'L2': 456.0})
        mgr.cooldown = {'L1': 1000.0}
        mgr.pending = {'L3': 'below'}
        mgr.save(now=1000.0)
        again = self.manager()
        self.assertEqual(again.cooldown, {'L1': 1000.0})
        self.assertEqual(again.pending, {'L3': 'below'})


class HysteresisTest(BaseCase):
    def test_oscillation_inside_band_does_not_alert(self):
        mgr = self.manager(cooldown=0)
        prices = [60900, 61010, 60990, 61050, 60950, 61000]  # 缓冲带 ±122
        prev = 60800
        for i, p in enumerate(prices):
            self.assertIsNone(mgr.check_level('L', prev, p, 61000, i), f'第{i}轮不应推送')
            prev = p

    def test_cross_through_band_over_several_runs(self):
        """先进入缓冲带、下一轮才越过缓冲带，也必须识别为突破。"""
        mgr = self.manager(cooldown=0)
        self.assertIsNone(mgr.check_level('L', 60800, 61010, 61000, 1))
        self.assertEqual(mgr.check_level('L', 61010, 61200, 61000, 2), '涨破')
        # 回到缓冲带再上去，不算新突破
        self.assertIsNone(mgr.check_level('L', 61200, 60990, 61000, 3))
        self.assertIsNone(mgr.check_level('L', 60990, 61300, 61000, 4))
        # 真正跌破缓冲带下沿
        self.assertEqual(mgr.check_level('L', 61300, 60800, 61000, 5), '跌破')


class BarkTest(BaseCase):
    def _resp(self, status, body):
        r = mock.Mock(status_code=status, text=json.dumps(body))
        r.json.return_value = body
        return r

    def test_post_json_and_check_code(self):
        self.m.BARK_API_KEYS = ['k1', 'k2']
        post = mock.Mock(side_effect=[self._resp(200, {'code': 200}), self._resp(400, {'code': 400})])
        self.m.requests.post = post
        msg = '现价 $61,000 / 涨破 #1 ?a=b 100%'
        self.assertFalse(self.m.send_bark_alert(msg))
        self.assertEqual(post.call_count, 2)
        _, kwargs = post.call_args_list[0]
        self.assertEqual(kwargs['json'], {'device_key': 'k1', 'title': '🚨 BTC行情提醒', 'body': msg})

    def test_failure_log_masks_key(self):
        self.m.BARK_API_KEYS = ['secretkey']
        self.m.requests.post = mock.Mock(return_value=self._resp(
            400, {'code': 400, 'message': 'failed to get [secretkey] device token'}))
        logs = []
        self.m.log = logs.append
        self.assertFalse(self.m.send_bark_alert('x'))
        self.assertTrue(logs)
        self.assertFalse(any('secretkey' in line for line in logs))

    def test_no_key_skips(self):
        self.m.BARK_API_KEYS = []
        self.m.requests.post = mock.Mock()
        self.assertFalse(self.m.send_bark_alert('x'))
        self.m.requests.post.assert_not_called()


class AlertModeTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.m.get_btc_ma = lambda days: None
        self.m.alert_manager = self.manager()
        self.sent = []
        self.m.send_bark_alert = lambda body, title='🚨 BTC行情提醒': self.sent.append((title, body)) or True

    def test_stale_cache_skips_detection(self):
        self.m.save_price(50000, now=0)  # 很久以前的价格
        self.m.get_btc_price = lambda: 62500
        self.m.run_alert_mode()
        self.assertEqual(self.sent, [])
        price, ts = self.m.load_prev_price()
        self.assertEqual(price, 62500)

    def test_old_plain_cache_treated_as_stale(self):
        with open(self.m.CACHE_PATH, 'w') as f:
            f.write('50000.0')
        self.m.get_btc_price = lambda: 62500
        self.m.run_alert_mode()
        self.assertEqual(self.sent, [])

    def test_fresh_cache_alerts_all_levels_once(self):
        import time
        self.m.save_price(60500, now=time.time())
        self.m.get_btc_price = lambda: 62500
        self.m.run_alert_mode()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('61,000', self.sent[0][1])
        self.assertIn('62,000', self.sent[0][1])

    def test_fetch_failure_alert_once_then_recovery(self):
        self.m.FAIL_ALERT_THRESHOLD = 3
        self.m.get_btc_price = lambda: None
        for _ in range(5):
            self.m.run_alert_mode()
        self.assertEqual([t for t, _ in self.sent], ['⚠️ BTC监控异常'])
        self.m.get_btc_price = lambda: 60000
        self.m.run_alert_mode()
        self.assertEqual(self.sent[-1][0], '✅ BTC监控恢复')
        self.assertEqual(self.m.alert_manager.fail_count, 0)


if __name__ == '__main__':
    unittest.main()
