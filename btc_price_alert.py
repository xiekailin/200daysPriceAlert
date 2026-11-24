import requests
import time
import os
import sys
import hmac
import math
import datetime
import json
import traceback

LOG_PATH = 'btc_alert.log'
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5MB

def log(msg):
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{now}] {msg}\n"
    
    # 先检查文件大小，如果超过限制就裁剪
    try:
        if os.path.exists(LOG_PATH):
            current_size = os.path.getsize(LOG_PATH)
            
            if current_size > MAX_LOG_SIZE:
                # 读取文件内容
                with open(LOG_PATH, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                # 保留最后约4MB的内容（留一些缓冲空间）
                target_size = 4 * 1024 * 1024
                current_size = 0
                kept_lines = []
                
                # 从后往前计算，保留不超过4MB的内容
                for line in reversed(lines):
                    line_size = len(line.encode('utf-8'))
                    if current_size + line_size > target_size:
                        break
                    kept_lines.insert(0, line)
                    current_size += line_size
                
                # 重写文件
                with open(LOG_PATH, 'w', encoding='utf-8') as f:
                    f.writelines(kept_lines)
                
                print(f"[日志裁剪] 文件大小超过{MAX_LOG_SIZE/1024/1024:.1f}MB，已裁剪至{current_size/1024/1024:.1f}MB")
    except Exception as e:
        print(f"[日志裁剪错误] {e}")
    
    # 写入新日志；磁盘满或无权限时只打印到标准错误，不能让日志失败拖垮推送和状态保存
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError as e:
        print(f"[日志写入错误] {e}", file=sys.stderr)
    print(line.strip())  # 保留原有print调试

def atomic_write(path, text):
    """先写临时文件再替换，避免进程中途退出留下半截缓存文件。"""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp_path, path)

# ====== 配置部分 ======
BARK_API_KEY = os.getenv('BARK_API_KEY', 'Znodd8yskndqUUbMVnmzBn')  # 多个key用英文逗号分隔；设置环境变量可覆盖默认key
BARK_API_KEYS = [k.strip() for k in BARK_API_KEY.split(',') if k.strip()]
BARK_PUSH_URL = 'https://api.day.app/push'
ALERT_PRICE = os.getenv('ALERT_PRICE')
USE_MA200 = os.getenv('USE_MA200', 'false').lower() == 'true'
ALERT_MODE = os.getenv('ALERT_MODE', 'alert')  # 'alert', 'report' 或 'web'
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '30'))  # 冷却时间（分钟）
HYSTERESIS_PCT = float(os.getenv('HYSTERESIS_PCT', '0.2'))  # 关口上下缓冲带（%），价格需越过缓冲带才算突破
PRICE_CACHE_MAX_AGE_MINUTES = int(os.getenv('PRICE_CACHE_MAX_AGE_MINUTES', '30'))  # 上次价格超过此时长视为过期
FAIL_ALERT_THRESHOLD = int(os.getenv('FAIL_ALERT_THRESHOLD', '6'))  # 连续取价失败多少次后推送异常告警
WEB_TOKEN = os.getenv('WEB_TOKEN', '')  # web 模式必填，调用 /report 时校验
PRICE_JUMP_PCT = float(os.getenv('PRICE_JUMP_PCT', '10'))  # 价格相对上次变动超过此比例（%）时，用第二个数据源核对
PRICE_CONFIRM_TOLERANCE_PCT = 1.0  # 两个数据源价格相差在此比例（%）以内视为一致
OUTAGE_ALERT_PCT = float(os.getenv('OUTAGE_ALERT_PCT', '3'))  # 监控中断恢复后，价格变化超过此比例（%）补推一条汇总
HTTP_TIMEOUT = (5, 10)  # (连接超时, 读取超时) 秒
CACHE_PATH = 'last_price.cache'
STATE_PATH = 'cooldown_cache.json'

# ====== 关口与均线列表 ======
IMPORTANT_LEVELS = list(range(40000, 1000000, 1000))
MA_LEVELS = [30, 90, 120]

log(f"[配置] ALERT_MODE: {ALERT_MODE}")
log(f"[配置] BARK_API_KEY: {'已设置' if BARK_API_KEY else '未设置'}")
log(f"[配置] USE_MA200: {USE_MA200}")
if ALERT_MODE == 'alert':
    log(f"[配置] ALERT_PRICE: {ALERT_PRICE}")

# ====== HTTP 与数据源 ======
_session = None

def get_session():
    """共享 HTTP 连接，避免每个请求都重新握手。
    GET 遇到连接失败或 5xx 自动重试一次；429 限流不重试也不等待 Retry-After，直接换下一个数据源。"""
    global _session
    if _session is None:
        session = requests.Session()
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry = Retry(
                total=1, connect=1, read=0, status=1,
                backoff_factor=0.5,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset(['GET']),
                raise_on_status=False,
                respect_retry_after_header=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount('https://', adapter)
            session.mount('http://', adapter)
        except (ImportError, TypeError) as e:
            # urllib3 低于 1.26 不支持 allowed_methods；退回不重试的会话，不能让取价和告警全部失效
            log(f"[警告] 当前 urllib3 不支持自动重试，已关闭重试: {describe_error(e)}")
        _session = session
    return _session

def http_get_json(url):
    """GET 并解析 JSON。先检查状态码，日志里能直接看出是限流（429）还是服务端故障（5xx）。"""
    resp = get_session().get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def describe_error(e):
    """带上异常类型，避免日志里只剩一个看不懂的 'bitcoin' / 'data'。"""
    return f"{type(e).__name__}: {e}"

def kraken_result(data):
    """Kraken 出错时 HTTP 仍返回 200，错误写在 error 字段里，这里转成可读的异常。"""
    if data.get('error'):
        raise ValueError(f"Kraken 返回错误: {data['error']}")
    return data['result']

def validate_price(value):
    """数据源偶尔返回 0、空值或非数字，直接当作这个数据源失败，换下一个。"""
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"价格数值异常: {value!r}")
    return price

# (名称, 地址, 从响应 JSON 中取出价格的函数)
PRICE_SOURCES = [
    ('Coinbase', 'https://api.coinbase.com/v2/prices/spot?currency=USD',
     lambda d: d['data']['amount']),
    ('Kraken', 'https://api.kraken.com/0/public/Ticker?pair=XBTUSD',
     lambda d: kraken_result(d)['XXBTZUSD']['c'][0]),
    ('Bitstamp', 'https://www.bitstamp.net/api/v2/ticker/btcusd/',
     lambda d: d['last']),
    ('CoinGecko', 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd',
     lambda d: d['bitcoin']['usd']),
    ('CryptoCompare', 'https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD',
     lambda d: d['USD']),
]

def fetch_price(skip=()):
    """按顺序尝试各数据源，返回 (价格, 数据源名称)；全部失败返回 (None, None)。
    skip 用于核对价格时排除已经用过的数据源。"""
    for name, url, parse in PRICE_SOURCES:
        if name in skip:
            continue
        try:
            price = validate_price(parse(http_get_json(url)))
            log(f"[数据] 通过 {name} 获取到价格: {price}")
            return price, name
        except Exception as e:
            log(f"[错误] {name} 获取价格失败: {describe_error(e)}")
    return None, None

def get_btc_price():
    return fetch_price()[0]

def get_btc_ma(days):
    def get_from_kraken():
        url = 'https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440&since=0'
        result = kraken_result(http_get_json(url))
        # Kraken might return more than `days` data, so we slice it
        closes = [float(item[4]) for item in list(result.values())[0] if isinstance(item, list)]
        closes = closes[-days:]
        return sum(closes) / len(closes)

    def get_from_bitstamp():
        url = f'https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit={days}'
        data = http_get_json(url)
        closes = [float(item['close']) for item in data['data']['ohlc']]
        return sum(closes) / len(closes)

    def get_from_coingecko():
        url = f'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily'
        data = http_get_json(url)
        if 'prices' not in data:
            raise Exception("No 'prices' in response")
        prices = [item[1] for item in data['prices']]
        # CoinGecko might return more than `days` data points
        if len(prices) > days:
            prices = prices[-days:]
        return sum(prices) / len(prices)

    apis = [
        ('Kraken', get_from_kraken),
        ('CoinGecko', get_from_coingecko),
        ('Bitstamp', get_from_bitstamp),
    ]

    for name, func in apis:
        try:
            ma = func()
            log(f"[结果] 通过 {name} 获取到 {days}日均线: {ma:.2f}")
            return ma
        except Exception as e:
            log(f"[错误] {name} 获取 MA({days}) 失败: {describe_error(e)}")

    log(f'[错误] 所有API获取{days}日均线均失败')
    return None

def get_btc_24h_change():
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true'
        return float(http_get_json(url)['bitcoin']['usd_24h_change'])
    except Exception as e:
        log(f"[错误] CoinGecko 获取24h涨跌幅失败: {describe_error(e)}")
        return None

# ====== 发送推送 ======
def send_bark_alert(body_msg, title='🚨 BTC行情提醒'):
    """通过 Bark 的 POST JSON 接口推送，避免消息里的 / # ? % 等字符破坏 URL。
    返回是否所有设备都推送成功（以 Bark 返回 code == 200 为准）。"""
    if not BARK_API_KEYS:
        log('[错误] 未配置 BARK_API_KEY，跳过推送')
        return False
    log(f"[推送] 标题: {title} | 内容: {body_msg}")
    all_ok = True
    for idx, key in enumerate(BARK_API_KEYS, 1):
        try:
            resp = get_session().post(
                BARK_PUSH_URL,
                json={'device_key': key, 'title': title, 'body': body_msg},
                timeout=HTTP_TIMEOUT,
            )
            try:
                code = resp.json().get('code')
            except ValueError:
                code = None
            if resp.status_code == 200 and code == 200:
                log(f'[推送] 第{idx}个设备推送成功')
            else:
                all_ok = False
                # Bark 的错误信息会原样带回 device key，写日志前脱敏
                log(f'[错误] 第{idx}个设备推送失败: HTTP {resp.status_code} {resp.text[:200].replace(key, "***")}')
        except Exception as e:
            all_ok = False
            log(f'[错误] 第{idx}个设备推送异常: {str(e).replace(key, "***")}')
    return all_ok

# ====== 运行模式 ======
def run_report_mode():
    log('[主流程] 进入定时报告模式...')
    price = get_btc_price()
    if price is None:
        msg = "❌ 获取BTC行情失败"
        send_bark_alert(msg)
        return msg

    change = get_btc_24h_change()
    if change is not None:
        change_emoji = "📈" if change >= 0 else "📉"
        report_msg = f"💰 现价: ${price:,.2f}\n{change_emoji} 24h涨跌: {change:+.2f}%"
    else:
        report_msg = f"💰 现价: ${price:,.2f}\n❓ 24h涨跌: N/A"

    all_ma_days = sorted(list(set(MA_LEVELS + ([200] if USE_MA200 else []))))
    for days in all_ma_days:
        ma = get_btc_ma(days)
        if ma:
            report_msg += f"\n📊 MA({days}): ${ma:,.0f}"
    log(f"[报告内容] {report_msg}")
    send_bark_alert(report_msg)
    return report_msg

class SmartAlertManager:
    """关口预警状态：冷却期、迟滞缓冲带、取价连续失败计数，统一持久化到 state_file。

    突破判断规则：关口上下各留 hysteresis_pct% 的缓冲带，价格必须离开缓冲带、
    且所在方向与上次确认的方向相反才算一次突破，避免价格贴着关口来回晃时反复推送。
    """

    def __init__(self, cooldown_minutes=15, hysteresis_pct=0.2, state_file=STATE_PATH):
        self.cooldown_seconds = int(cooldown_minutes * 60)
        self.band_ratio = hysteresis_pct / 100
        self.state_file = state_file
        self.cooldown = {}  # {关口名: 上次推送时间戳}，每个关口独立计时
        # {关口名: "above"/"below"}：当前停在缓冲带内的关口，记录进入缓冲带之前确认的方向。
        # 不在缓冲带内的关口无需记录，其方向可由上次价格直接推出。
        self.pending = {}
        self.fail_count = 0
        self.fail_alerted = False
        self.load()

    def load(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            log(f"[警告] 加载预警状态失败: {e}")
            return
        if not isinstance(data, dict):
            return
        if data.get('version') == 2:
            self.cooldown = {k: float(v) for k, v in data.get('cooldown', {}).items()}
            self.pending = dict(data.get('pending', {}))
            self.fail_count = int(data.get('fail_count', 0))
            self.fail_alerted = bool(data.get('fail_alerted', False))
        else:
            # 兼容旧格式 {关口名: {"time": ts, "state": ...}} 或 {关口名: ts}
            for name, value in data.items():
                ts = value.get('time') if isinstance(value, dict) else value
                if isinstance(ts, (int, float)):
                    self.cooldown[name] = float(ts)
        log(f"[缓存] 加载预警状态: 冷却中 {len(self.cooldown)} 个，缓冲带内 {len(self.pending)} 个")

    def save(self, now=None):
        now = time.time() if now is None else now
        # 过期的冷却记录没有意义，保存前清理掉
        self.cooldown = {k: v for k, v in self.cooldown.items() if now - v < self.cooldown_seconds}
        data = {
            'version': 2,
            'cooldown': self.cooldown,
            'pending': self.pending,
            'fail_count': self.fail_count,
            'fail_alerted': self.fail_alerted,
        }
        try:
            atomic_write(self.state_file, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            log(f"[错误] 保存预警状态失败: {e}")

    def reset_pending(self):
        """历史价格缺失或过期时调用：旧的方向记录已不可信，全部丢弃。"""
        self.pending = {}

    def side_of(self, price, level):
        band = level * self.band_ratio
        if price >= level + band:
            return 'above'
        if price <= level - band:
            return 'below'
        return None  # 处于缓冲带内

    def check_level(self, name, prev_price, current_price, level, now=None):
        """返回 "涨破" / "跌破" / None。冷却期内的突破只更新方向，不推送。"""
        now = time.time() if now is None else now
        reference = self.pending.get(name) or ('above' if prev_price >= level else 'below')
        current_side = self.side_of(current_price, level)
        if current_side is None:
            self.pending[name] = reference
            return None
        self.pending.pop(name, None)
        if current_side == reference:
            return None

        direction = '涨破' if current_side == 'above' else '跌破'
        last_time = self.cooldown.get(name)
        if last_time is not None and now - last_time < self.cooldown_seconds:
            remain = int(self.cooldown_seconds - (now - last_time))
            log(f"[冷却期中] {name} {direction} 跳过推送 | 剩余: {remain}s/共{self.cooldown_seconds}s")
            return None
        self.cooldown[name] = now
        log(f"[突破] {name} {direction}，进入冷却期 {self.cooldown_seconds}s")
        return direction

# 创建智能预警管理器
alert_manager = SmartAlertManager(cooldown_minutes=COOLDOWN_MINUTES, hysteresis_pct=HYSTERESIS_PCT)

def load_prev_price():
    """返回 (上次价格, 记录时间戳)。兼容旧格式（只有价格），此时时间戳为 None。"""
    if not os.path.exists(CACHE_PATH):
        return None, None
    try:
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
            data = json.loads(f.read().strip())
        if isinstance(data, dict):
            return float(data['price']), float(data['time'])
        return float(data), None
    except Exception as e:
        log(f"[警告] 读取价格缓存失败: {e}")
        return None, None

def save_price(price, now=None):
    now = time.time() if now is None else now
    atomic_write(CACHE_PATH, json.dumps({'price': price, 'time': now}))

def verify_price(reference_price, price, source):
    """价格相对参考价变动超过 PRICE_JUMP_PCT 时，用另一个数据源核对，防止单个数据源的异常值引发大面积误报。
    返回可信的价格；无法确认时返回 None，本轮应跳过。"""
    change_pct = abs(price / reference_price - 1) * 100
    if change_pct <= PRICE_JUMP_PCT:
        return price
    log(f"[校验] {source} 价格 ${price:,.2f} 相对上次 ${reference_price:,.2f} 变动 {change_pct:.1f}%，"
        f"超过 {PRICE_JUMP_PCT}%，用其他数据源核对")
    other, other_source = fetch_price(skip={source})
    if other is None:
        log('[校验] 没有其他可用数据源，无法确认，本轮跳过')
        return None
    if abs(other / price - 1) * 100 <= PRICE_CONFIRM_TOLERANCE_PCT:
        log(f"[校验] {other_source} 价格 ${other:,.2f} 与之一致，确认有效")
        return price
    if abs(other / reference_price - 1) * 100 <= PRICE_JUMP_PCT:
        # 第二个数据源的价格和上次接近，说明是第一个数据源出了问题
        log(f"[校验] {source} 价格疑似异常，改用 {other_source} 价格 ${other:,.2f}")
        return other
    log(f"[校验] {source} ${price:,.2f} 与 {other_source} ${other:,.2f} 差异过大，无法确认，本轮跳过")
    return None

def format_duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"

# 🔺 表示涨破（通常为红色），📉 表示跌破（通常为绿色）
DIRECTION_EMOJI = {'涨破': '🔺', '跌破': '📉'}

def build_alert_reasons(events):
    """把本轮触发的关口整理成推送文案，顺序为 固定阈值 → 整数关口 → 均线。
    同方向的多个整数关口合并成一条，避免大行情时一条通知挤满几十个关口。
    events: [(关口名, 价位, 方向)]"""
    fixed, ma = [], []
    integers = {'涨破': [], '跌破': []}
    for name, level, direction in events:
        emoji = DIRECTION_EMOJI[direction]
        if name.startswith('整数关口'):
            integers[direction].append(level)
        elif name.startswith('MA('):
            # 为MA均线添加价格信息到显示中
            ma.append(f"{emoji} {direction} {name} ${level:,.0f}")
        else:
            fixed.append(f"{emoji} {direction} {name}")

    merged = []
    for direction, levels in integers.items():
        if not levels:
            continue
        # 涨破从低到高、跌破从高到低排列，和价格运动方向一致
        levels = sorted(levels, reverse=(direction == '跌破'))
        emoji = DIRECTION_EMOJI[direction]
        if len(levels) == 1:
            merged.append(f"{emoji} {direction} 整数关口 ${levels[0]:,}")
        else:
            merged.append(f"{emoji} 连续{direction} {len(levels)} 个整数关口（${levels[0]:,} → ${levels[-1]:,}）")
    return fixed + merged + ma

def run_alert_mode():
    log('[主流程] 进入关键点位预警模式...')
    prev_price, prev_time = load_prev_price()
    if prev_price is not None:
        log(f"[缓存] 读取到上次价格: {prev_price:,.2f}")

    current_price, source = fetch_price()
    if current_price is None:
        alert_manager.fail_count += 1
        log(f"[错误] 本次未能获取价格，跳过（已连续失败 {alert_manager.fail_count} 次）")
        if alert_manager.fail_count >= FAIL_ALERT_THRESHOLD and not alert_manager.fail_alerted:
            # 推送成功才标记已告警，失败则下次继续尝试
            if send_bark_alert(f"已连续 {alert_manager.fail_count} 次获取BTC价格失败，请检查服务器网络或数据源",
                               title='⚠️ BTC监控异常'):
                alert_manager.fail_alerted = True
        alert_manager.save()
        return

    recovered = alert_manager.fail_alerted
    alert_manager.fail_count = 0
    alert_manager.fail_alerted = False

    # 价格合理性校验：变动过大先核对，无法确认就跳过本轮，且不覆盖上次的可信价格
    if prev_price is not None:
        current_price = verify_price(prev_price, current_price, source)
        if current_price is None:
            if recovered:
                send_bark_alert('价格获取已恢复，但当前价格未通过校验，本轮跳过', title='✅ BTC监控恢复')
            alert_manager.save()
            return

    now = time.time()
    max_age = PRICE_CACHE_MAX_AGE_MINUTES * 60
    is_fresh = prev_price is not None and prev_time is not None and now - prev_time <= max_age

    # 监控中断（缓存过期）期间价格变化较大时补推一条汇总，避免中断期间的大行情被悄悄跳过
    gap_msg = None
    if not is_fresh and prev_price is not None and prev_time is not None:
        change_pct = (current_price / prev_price - 1) * 100
        if abs(change_pct) >= OUTAGE_ALERT_PCT:
            emoji = '📈' if change_pct > 0 else '📉'
            gap_msg = (f"{emoji} 监控中断约 {format_duration(now - prev_time)}，"
                       f"期间价格 ${prev_price:,.2f} → ${current_price:,.2f}（{change_pct:+.2f}%）")

    if recovered:
        recover_msg = f"价格获取已恢复，现价 ${current_price:,.2f}"
        send_bark_alert(f"{recover_msg}\n{gap_msg}" if gap_msg else recover_msg, title='✅ BTC监控恢复')
    elif gap_msg:
        send_bark_alert(gap_msg)

    if not is_fresh:
        reason = '无历史价格' if prev_price is None else f'历史价格缺少时间戳或已超过 {PRICE_CACHE_MAX_AGE_MINUTES} 分钟'
        log(f"[缓存] {reason}，保存当前价 {current_price:,.2f} 后退出，本次不做逐个关口判断。")
        alert_manager.reset_pending()
        alert_manager.save(now)
        save_price(current_price, now)
        return

    levels_to_check = {}

    # 1. 固定价格阈值 (仅在不使用MA200时生效)
    fixed_price = None
    if not USE_MA200 and ALERT_PRICE:
        try:
            fixed_price = float(ALERT_PRICE)
            levels_to_check[f'固定阈值 ${fixed_price:,.0f}'] = fixed_price
        except (ValueError, TypeError):
            log(f"[警告] 无效的 ALERT_PRICE 值: {ALERT_PRICE}")

    # 2. 整数关口（与固定阈值同价位时只保留固定阈值，避免同一价位推两次）
    for level in IMPORTANT_LEVELS:
        if level == fixed_price:
            continue
        levels_to_check[f'整数关口 ${level:,}'] = level

    # 3. MA均线 (包含MA200, 如果启用)
    all_ma_days = sorted(list(set(MA_LEVELS + ([200] if USE_MA200 else []))))
    for days in all_ma_days:
        ma = get_btc_ma(days)
        if ma:
            levels_to_check[f'MA({days})'] = ma

    events = []
    for name, level in levels_to_check.items():
        direction = alert_manager.check_level(name, prev_price, current_price, level, now)
        if direction:
            events.append((name, level, direction))

    alert_manager.save(now)

    alert_reasons = build_alert_reasons(events)
    if alert_reasons:
        # 将所有预警理由在一行内展示
        alert_msg = f"💰 现价: ${current_price:,.2f}  " + "  ".join(alert_reasons)
        log(f"[预警] {alert_msg}")
        send_bark_alert(alert_msg)
    else:
        log(f"[主流程] 价格从 ${prev_price:,.2f} -> ${current_price:,.2f}, 未触发有效预警。")

    save_price(current_price, now)

def run_web_mode():
    # 只在 web 模式需要 flask，延迟导入，避免 cron 跑 alert/report 时因未安装 flask 而崩溃
    from flask import Flask, jsonify, request

    if not WEB_TOKEN:
        log('[错误] web 模式必须设置 WEB_TOKEN 环境变量，否则任何人都能触发推送')
        sys.exit(1)

    app = Flask(__name__)

    @app.route('/')
    def index():
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>BTC Price Alert</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body { font-family: sans-serif; text-align: center; padding-top: 50px; background-color: #f4f4f4; color: #333; }
                h1 { color: #0056b3; }
                button { font-size: 1.5em; padding: 15px 30px; cursor: pointer; background-color: #007bff; color: white; border: none; border-radius: 5px; transition: background-color 0.3s; }
                button:disabled { background-color: #cccccc; }
                button:hover:not(:disabled) { background-color: #0056b3; }
                #result { margin-top: 30px; font-size: 1.2em; white-space: pre-wrap; background-color: #fff; padding: 20px; border-radius: 5px; display: inline-block; text-align: left; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
            </style>
        </head>
        <body>
            <h1>BTC 价格推送</h1>
            <p><input id="token" type="password" placeholder="访问令牌 WEB_TOKEN" style="font-size:1.1em;padding:8px;"></p>
            <button id="reportButton" onclick="getReport()">推送当前价格</button>
            <div id="result"></div>
            <script>
                // 令牌只保存在本机浏览器，不放进 URL，避免出现在访问日志和历史记录里
                const tokenInput = document.getElementById('token');
                tokenInput.value = localStorage.getItem('btcAlertToken') || '';

                function getReport() {
                    localStorage.setItem('btcAlertToken', tokenInput.value);
                    const button = document.getElementById('reportButton');
                    const resultDiv = document.getElementById('result');
                    button.disabled = true;
                    button.innerText = '正在推送...';
                    resultDiv.innerText = '';

                    fetch('/report', { method: 'POST', headers: { 'X-Token': tokenInput.value } })
                        .then(response => response.json())
                        .then(data => {
                            resultDiv.innerText = data.message;
                        })
                        .catch(error => {
                            resultDiv.innerText = '错误: ' + error;
                        })
                        .finally(() => {
                            button.disabled = false;
                            button.innerText = '推送当前价格';
                        });
                }
            </script>
        </body>
        </html>
        """

    @app.route('/report', methods=['POST'])
    def report():
        token = request.headers.get('X-Token', '')
        if not hmac.compare_digest(token.encode('utf-8'), WEB_TOKEN.encode('utf-8')):
            log('[Web] 拒绝令牌错误的推送请求')
            return jsonify({"message": "令牌错误，未推送"}), 401
        log('[Web] 收到推送请求')
        report_message = run_report_mode()
        return jsonify({"message": report_message})

    port = 5002
    log(f'[Web] Web服务已启动，请访问 http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port)

# ====== 异常兜底 ======
def crash_marker_path():
    # 按模式区分，避免 report 模式的异常被 alert 模式的正常运行误判为"已恢复"
    return f'crash_alert_{ALERT_MODE}.json'

def notify_crash(exc):
    """未捕获异常：记录完整堆栈并推送告警。同一位置的同类异常只推一次，直到恢复正常。"""
    log(f"[崩溃] 未捕获异常:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")
    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else '未知位置'
    # 用异常类型 + 出错位置做标识，异常消息里常带时间、数值，不适合用来判断是否同一问题
    signature = f"{type(exc).__name__}@{where}"
    marker = crash_marker_path()
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            last = json.load(f)
        if isinstance(last, dict) and last.get('signature') == signature:
            log('[崩溃] 同一异常已推送过告警，本次不重复推送')
            return
    except (OSError, ValueError):
        pass  # 没有标记或标记损坏：按首次异常处理
    body = f"{ALERT_MODE} 模式运行异常：{type(exc).__name__}: {str(exc)[:200]}（{where}），详见日志"
    if send_bark_alert(body, title='⚠️ BTC脚本异常'):
        try:
            atomic_write(marker, json.dumps({'signature': signature, 'time': time.time()}, ensure_ascii=False))
        except OSError as e:
            log(f"[错误] 写入异常标记失败: {e}")

def clear_crash_marker():
    """本次正常跑完：如果之前推送过异常告警，推一条恢复通知并清除标记（推送失败则下次再试）。"""
    marker = crash_marker_path()
    if not os.path.exists(marker):
        return
    if send_bark_alert(f"{ALERT_MODE} 模式已恢复正常运行", title='✅ BTC脚本恢复'):
        try:
            os.remove(marker)
        except OSError as e:
            log(f"[错误] 删除异常标记失败: {e}")

def main():
    if not BARK_API_KEYS:
        log('[错误] 未设置 BARK_API_KEY 环境变量（多个用英文逗号分隔），无法推送，退出')
        sys.exit(1)
    runners = {'report': run_report_mode, 'alert': run_alert_mode, 'web': run_web_mode}
    runner = runners.get(ALERT_MODE)
    if runner is None:
        # 抛出异常走兜底流程：推送告警并以非 0 退出码结束，cron 写错模式时也能发现
        raise ValueError(f"未知的ALERT_MODE: {ALERT_MODE}")
    runner()
    clear_crash_marker()

def run():
    try:
        main()
    except Exception as exc:  # SystemExit / KeyboardInterrupt 不属于 Exception，仍按原样退出
        notify_crash(exc)
        sys.exit(1)

if __name__ == '__main__':
    run()
