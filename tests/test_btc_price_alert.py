"""btc_price_alert 回归测试（仅用标准库 unittest，网络请求全部打桩）。

运行：python3 -m unittest discover -s tests -v
"""
import importlib
import json
import os
import sys
import tempfile
import time
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


class BlockedSession:
    def get(self, *a, **k):
        raise AssertionError('测试中不应访问网络')

    post = get


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'{self.status_code} Client Error: Too Many Requests for url: fake')


class FakeSession:
    """按 URL 关键字返回预设响应的假会话。"""

    def __init__(self, routes=None, post_responses=None):
        self.routes = routes or {}
        self.post = mock.Mock(side_effect=post_responses or [])
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        for keyword, resp in self.routes.items():
            if keyword in url:
                return resp
        raise ConnectionError(f'无路由: {url}')


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.m = load_module(self._tmp.name)
        self.m.print = lambda *a, **k: None  # 测试时不刷屏
        # 默认屏蔽一切真实网络请求（代码里带有真实的默认 Bark key）
        self.m.get_session = lambda: BlockedSession()

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
    def test_post_json_and_check_code(self):
        self.m.BARK_API_KEYS = ['k1', 'k2']
        session = FakeSession(post_responses=[FakeResponse(200, {'code': 200}), FakeResponse(400, {'code': 400})])
        self.m.get_session = lambda: session
        msg = '现价 $61,000 / 涨破 #1 ?a=b 100%'
        self.assertFalse(self.m.send_bark_alert(msg))
        self.assertEqual(session.post.call_count, 2)
        _, kwargs = session.post.call_args_list[0]
        self.assertEqual(kwargs['json'], {'device_key': 'k1', 'title': '🚨 BTC行情提醒', 'body': msg})

    def test_failure_log_masks_key(self):
        self.m.BARK_API_KEYS = ['secretkey']
        session = FakeSession(post_responses=[
            FakeResponse(400, {'code': 400, 'message': 'failed to get [secretkey] device token'})])
        self.m.get_session = lambda: session
        logs = []
        self.m.log = logs.append
        self.assertFalse(self.m.send_bark_alert('x'))
        self.assertTrue(logs)
        self.assertFalse(any('secretkey' in line for line in logs))

    def test_no_key_skips(self):
        self.m.BARK_API_KEYS = []
        session = FakeSession()
        self.m.get_session = lambda: session
        self.assertFalse(self.m.send_bark_alert('x'))
        session.post.assert_not_called()


class DataSourceTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.logs = []
        self.m.log = self.logs.append

    def test_invalid_price_falls_back_to_next_source(self):
        """数据源返回 0 时视为失败，自动换下一个数据源。"""
        self.m.get_session = lambda: FakeSession({
            'coinbase': FakeResponse(200, {'data': {'amount': '0'}}),
            'kraken': FakeResponse(200, {'error': [], 'result': {'XXBTZUSD': {'c': ['61000.5', '1']}}}),
        })
        self.assertEqual(self.m.fetch_price(), (61000.5, 'Kraken'))
        self.assertTrue(any('价格数值异常' in line for line in self.logs))

    def test_http_status_and_kraken_error_are_readable(self):
        self.m.get_session = lambda: FakeSession({
            'coinbase': FakeResponse(429, {'message': 'rate limited'}),
            'kraken': FakeResponse(200, {'error': ['EGeneral:Too many requests']}),
        })
        self.m.PRICE_SOURCES = self.m.PRICE_SOURCES[:2]
        self.assertEqual(self.m.fetch_price(), (None, None))
        text = '\n'.join(self.logs)
        self.assertIn('429', text)
        self.assertIn('Kraken 返回错误', text)
        self.assertIn('EGeneral:Too many requests', text)

    def test_skip_excludes_source(self):
        session = FakeSession({'kraken': FakeResponse(200, {'error': [], 'result': {'XXBTZUSD': {'c': ['1']}}})})
        self.m.get_session = lambda: session
        self.m.fetch_price(skip={'Coinbase'})
        self.assertFalse(any('coinbase' in u for u in session.urls))


class MessageTest(BaseCase):
    def test_merge_integer_levels_by_direction(self):
        events = [
            ('固定阈值 $65,500', 65500.0, '跌破'),
            ('整数关口 $63,000', 63000, '跌破'),
            ('整数关口 $65,000', 65000, '跌破'),
            ('整数关口 $64,000', 64000, '跌破'),
            ('MA(30)', 64321.4, '跌破'),
        ]
        self.assertEqual(self.m.build_alert_reasons(events), [
            '📉 跌破 固定阈值 $65,500',
            '📉 连续跌破 3 个整数关口（$65,000 → $63,000）',
            '📉 跌破 MA(30) $64,321',
        ])

    def test_single_integer_level_and_rise_order(self):
        self.assertEqual(self.m.build_alert_reasons([('整数关口 $61,000', 61000, '涨破')]),
                         ['🔺 涨破 整数关口 $61,000'])
        events = [('整数关口 $62,000', 62000, '涨破'), ('整数关口 $61,000', 61000, '涨破')]
        self.assertEqual(self.m.build_alert_reasons(events), ['🔺 连续涨破 2 个整数关口（$61,000 → $62,000）'])

    def test_format_duration(self):
        self.assertEqual(self.m.format_duration(45 * 60), '45 分钟')
        self.assertEqual(self.m.format_duration(2 * 3600), '2 小时')
        self.assertEqual(self.m.format_duration(3 * 3600 + 5 * 60), '3 小时 5 分钟')
        self.assertEqual(self.m.format_duration(26 * 3600), '1 天 2 小时')


class AlertModeTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.m.get_btc_ma = lambda days: None
        self.m.alert_manager = self.manager()
        self.sent = []
        self.m.send_bark_alert = lambda body, title='🚨 BTC行情提醒': self.sent.append((title, body)) or True

    def price(self, primary, others=None):
        """primary：首选数据源价格；others：排除首选后其他数据源返回的价格。"""
        def fake(skip=()):
            if not skip:
                return (primary, 'Coinbase') if primary is not None else (None, None)
            return (others, 'Kraken') if others is not None else (None, None)
        self.m.fetch_price = fake

    def save_recent(self, price, minutes_ago=5):
        self.m.save_price(price, now=time.time() - minutes_ago * 60)

    def test_stale_cache_skips_detection_and_sends_gap_summary(self):
        """缓存过期：不逐个判断关口，但价格变化较大时补推一条中断汇总。"""
        self.save_recent(60000, minutes_ago=125)
        self.price(63000)  # +5%
        self.m.run_alert_mode()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('监控中断约 2 小时 5 分钟', self.sent[0][1])
        self.assertIn('$60,000.00 → $63,000.00（+5.00%）', self.sent[0][1])
        self.assertNotIn('整数关口', self.sent[0][1])
        self.assertEqual(self.m.load_prev_price()[0], 63000)

    def test_stale_cache_small_move_no_summary(self):
        self.save_recent(60000, minutes_ago=125)
        self.price(60600)  # +1%
        self.m.run_alert_mode()
        self.assertEqual(self.sent, [])

    def test_old_plain_cache_treated_as_stale_without_summary(self):
        with open(self.m.CACHE_PATH, 'w') as f:
            f.write('60000.0')
        self.price(63000)
        self.m.run_alert_mode()
        self.assertEqual(self.sent, [])  # 不知道中断了多久，不做汇总

    def test_fresh_cache_merges_integer_levels(self):
        self.save_recent(60500)
        self.price(62500)
        self.m.run_alert_mode()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('连续涨破 2 个整数关口（$61,000 → $62,000）', self.sent[0][1])

    def test_fixed_threshold_dedup_with_integer_level(self):
        self.m.ALERT_PRICE = '61000'
        self.save_recent(60500)
        self.price(61500)
        self.m.run_alert_mode()
        body = self.sent[0][1]
        self.assertIn('涨破 固定阈值 $61,000', body)
        self.assertNotIn('整数关口', body)

    def test_jump_rejected_when_other_source_disagrees(self):
        """首选数据源暴涨 50%，另一个数据源与上次价格接近：改用另一个数据源，不误报。"""
        self.save_recent(60500)
        self.price(90000, others=60550)
        self.m.run_alert_mode()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.m.load_prev_price()[0], 60550)

    def test_jump_confirmed_by_other_source(self):
        self.save_recent(60500)
        self.price(54000, others=54100)  # 真实暴跌约 10.7%
        self.m.run_alert_mode()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('连续跌破 6 个整数关口（$60,000 → $55,000）', self.sent[0][1])

    def test_jump_unconfirmed_skips_and_keeps_cache(self):
        self.save_recent(60500)
        before = self.m.load_prev_price()
        self.price(90000, others=None)  # 没有其他数据源可核对
        self.m.run_alert_mode()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.m.load_prev_price(), before)

    def test_fetch_failure_alert_once_then_recovery_with_gap(self):
        self.m.FAIL_ALERT_THRESHOLD = 3
        self.save_recent(60000, minutes_ago=40)
        self.price(None)
        for _ in range(5):
            self.m.run_alert_mode()
        self.assertEqual([t for t, _ in self.sent], ['⚠️ BTC监控异常'])
        self.price(56000)  # 中断期间跌了约 6.7%
        self.m.run_alert_mode()
        self.assertEqual(len(self.sent), 2)  # 恢复通知和中断汇总合并为一条
        title, body = self.sent[-1]
        self.assertEqual(title, '✅ BTC监控恢复')
        self.assertIn('价格获取已恢复', body)
        self.assertIn('监控中断约 40 分钟', body)
        self.assertEqual(self.m.alert_manager.fail_count, 0)


class CrashGuardTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.m.ALERT_MODE = 'alert'
        self.m.BARK_API_KEYS = ['k']
        self.sent = []
        self.m.send_bark_alert = lambda body, title='🚨 BTC行情提醒': self.sent.append((title, body)) or True

    def boom(self):
        raise RuntimeError('磁盘写入失败')

    def test_crash_alert_once_then_recover(self):
        self.m.run_alert_mode = self.boom
        for _ in range(3):
            with self.assertRaises(SystemExit) as ctx:
                self.m.run()
            self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(len(self.sent), 1)
        title, body = self.sent[0]
        self.assertEqual(title, '⚠️ BTC脚本异常')
        self.assertIn('RuntimeError: 磁盘写入失败', body)

        self.m.run_alert_mode = lambda: None
        self.m.run()
        self.assertEqual(self.sent[-1][0], '✅ BTC脚本恢复')
        self.assertFalse(os.path.exists(self.m.crash_marker_path()))
        self.m.run()
        self.assertEqual(len(self.sent), 2)  # 恢复后不再重复推送

    def test_crash_alert_retried_when_push_failed(self):
        self.m.run_alert_mode = self.boom
        self.m.send_bark_alert = lambda body, title='': self.sent.append(title) and False
        for _ in range(2):
            with self.assertRaises(SystemExit):
                self.m.run()
        self.assertEqual(len(self.sent), 2)

    def test_unknown_mode_exits_nonzero_and_alerts(self):
        self.m.ALERT_MODE = 'bogus'
        with self.assertRaises(SystemExit) as ctx:
            self.m.run()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn('未知的ALERT_MODE: bogus', self.sent[0][1])


if __name__ == '__main__':
    unittest.main()
