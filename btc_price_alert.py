import requests
import time
import os
import datetime
import json

LOG_PATH = 'btc_alert.log'
MAX_LOG_SIZE = 20 * 1024 * 1024  # 20MB

def log(msg):
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{now}] {msg}\n"
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line)
    print(line.strip())  # 保留原有print调试
    # 裁剪日志
    try:
        if os.path.getsize(LOG_PATH) > MAX_LOG_SIZE:
            with open(LOG_PATH, 'rb') as f:
                f.seek(-MAX_LOG_SIZE, os.SEEK_END)
                data = f.read()
            with open(LOG_PATH, 'wb') as f:
                f.write(data)
            # 可能会截断一行，补一个换行
            with open(LOG_PATH, 'ab') as f:
                f.write(b'\n')
    except Exception as e:
        print(f"[日志裁剪错误] {e}")

# ====== 配置部分 ======
BARK_API_KEY = os.getenv('BARK_API_KEY', 'Znodd8yskndqUUbMVnmzBn') # 多个key用英文逗号分隔
BARK_API_KEYS = [k.strip() for k in BARK_API_KEY.split(',') if k.strip()]
BARK_API_URL_TEMPLATE = 'https://api.day.app/{}/'
ALERT_PRICE = os.getenv('ALERT_PRICE')
USE_MA200 = os.getenv('USE_MA200', 'false').lower() == 'true'
ALERT_MODE = os.getenv('ALERT_MODE', 'alert')  # 'alert' 或 'report'
CACHE_PATH = 'last_price.cache'

# ====== 关口与均线列表 ======
IMPORTANT_LEVELS = list(range(100000, 200001, 1000))
MA_LEVELS = [30, 90, 120]

log(f"[配置] ALERT_MODE: {ALERT_MODE}")
log(f"[配置] BARK_API_KEY: {'已设置' if BARK_API_KEY else '未设置'}")
log(f"[配置] USE_MA200: {USE_MA200}")
if ALERT_MODE == 'alert':
    log(f"[配置] ALERT_PRICE: {ALERT_PRICE}")

# ====== API获取函数 (与之前类似，保持不变) ======
def get_btc_price():
    apis = [
        ('Coinbase', lambda: float(requests.get('https://api.coinbase.com/v2/prices/spot?currency=USD', timeout=10).json()['data']['amount'])),
        ('Kraken', lambda: float(requests.get('https://api.kraken.com/0/public/Ticker?pair=XBTUSD', timeout=10).json()['result']['XXBTZUSD']['c'][0])),
        ('Bitstamp', lambda: float(requests.get('https://www.bitstamp.net/api/v2/ticker/btcusd/', timeout=10).json()['last'])),
        ('CoinGecko', lambda: float(requests.get('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd', timeout=10).json()['bitcoin']['usd'])),
        ('CryptoCompare', lambda: float(requests.get('https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD', timeout=10).json()['USD'])),
    ]
    for name, func in apis:
        try:
            price = func()
            log(f"[数据] 通过 {name} 获取到价格: {price}")
            return price
        except Exception as e:
            log(f"[错误] {name} 获取价格失败: {e}")
    return None

def get_btc_ma(days):
    # Bitstamp
    try:
        url = f'https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit={days}'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item['close']) for item in data['data']['ohlc']]
        ma = sum(closes) / len(closes)
        log(f"[结果] Bitstamp {days}日均线: {ma:.2f}")
        return ma
    except Exception as e:
        log(f"[错误] Bitstamp 获取 MA({days}) 失败: {e}")
    # CoinGecko
    try:
        url = f'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if 'prices' not in data:
            log(f"[错误] CoinGecko 返回数据缺少 'prices' 字段: {data}")
            raise Exception("No 'prices' in response")
        prices = [item[1] for item in data['prices']]
        if len(prices) > days:
            prices = prices[-days:]
        ma = sum(prices) / len(prices)
        log(f"[结果] CoinGecko {days}日均线: {ma:.2f}")
        return ma
    except Exception as e:
        log(f"[错误] CoinGecko 获取 MA({days}) 失败: {e}")
    # Kraken
    try:
        url = f'https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440&since=0'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item[4]) for item in list(data['result'].values())[0] if isinstance(item, list)]
        closes = closes[-days:]
        ma = sum(closes) / len(closes)
        log(f"[结果] Kraken {days}日均线: {ma:.2f}")
        return ma
    except Exception as e:
        log(f"[错误] Kraken 获取 MA({days}) 失败: {e}")
    # CryptoCompare（仅支持现价，不支持历史K线，无法做均线）
    log(f'[错误] 所有API获取{days}日均线均失败')
    return None

def get_btc_24h_change():
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true'
        resp = requests.get(url, timeout=10)
        return float(resp.json()['bitcoin']['usd_24h_change'])
    except Exception as e:
        log(f"[错误] CoinGecko 获取24h涨跌幅失败: {e}")
        return None

# ====== 发送推送 ======
def send_bark_alert(body_msg):
    title = '🚨 BTC行情提醒'
    for key in BARK_API_KEYS:
        url = f"{BARK_API_URL_TEMPLATE.format(key)}{title}/{body_msg}"
        log(f"[推送] 发送Bark通知: {url.replace(key, '***')}")
        try:
            requests.get(url, timeout=10)
            log('[推送] 通知已发送')
        except Exception as e:
            log(f'[错误] 推送失败: {e}')

# ====== 运行模式 ======
def run_report_mode():
    log('[主流程] 进入定时报告模式...')
    price = get_btc_price()
    if price is None:
        send_bark_alert("❌ 获取BTC行情失败")
        return

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

class SmartAlertManager:
    def __init__(self, cooldown_minutes=15, breakthrough_threshold=0.2):
        self.cooldown_minutes = cooldown_minutes
        self.breakthrough_threshold = breakthrough_threshold  # 突破确认阈值（百分比）
        self.cooldown_file = 'cooldown_cache.json'  # 冷却期缓存文件
        self.price_states = {}     # 记录每个关口的当前状态
        self.last_alert_time = self.load_cooldown_data()  # 只加载一次
    
    def load_cooldown_data(self):
        """从文件加载冷却期数据"""
        try:
            if os.path.exists(self.cooldown_file):
                with open(self.cooldown_file, 'r') as f:
                    data = json.load(f)
                    log(f"[缓存] 加载冷却期数据: {len(data)} 个关口")
                    return data
        except Exception as e:
            log(f"[警告] 加载冷却期数据失败: {e}")
        return {}
    
    def save_cooldown_data(self):
        """保存冷却期数据到文件"""
        try:
            with open(self.cooldown_file, 'w') as f:
                json.dump(self.last_alert_time, f, indent=2)
            log(f"[缓存] 保存冷却期数据: {len(self.last_alert_time)} 个关口")
        except Exception as e:
            log(f"[错误] 保存冷却期数据失败: {e}")
    
    def can_alert(self, level_name, prev_price, current_price, level):
        """综合判断是否可以推送预警"""
        current_time = time.time()
        last_alert_time = self.last_alert_time
        
        # 判断是否真正突破
        is_breakthrough, direction = self.is_real_breakthrough(prev_price, current_price, level)
        if not is_breakthrough:
            return False, None
        
        # 检查冷却期（不分方向）
        if level_name in last_alert_time:
            time_diff = current_time - last_alert_time[level_name]
            if time_diff < self.cooldown_minutes * 60:
                log(f"[防骚扰] {level_name} 在冷却期内，跳过推送")
                return False, None
        
        # 推送并刷新冷却
        last_alert_time[level_name] = current_time
        self.save_cooldown_data()
        return True, direction
    
    def is_real_breakthrough(self, prev_price, current_price, level):
        """判断是否真正突破，需要穿越一定距离"""
        threshold = level * self.breakthrough_threshold / 100
        
        if prev_price < level and current_price > level + threshold:
            return True, "涨破"
        elif prev_price > level and current_price < level - threshold:
            return True, "跌破"
        return False, None
    
    def get_price_state(self, current_price, level):
        """获取价格相对于关口的状态"""
        if current_price > level + (level * 0.1 / 100):  # 高于关口0.1%
            return "above"
        elif current_price < level - (level * 0.1 / 100):  # 低于关口0.1%
            return "below"
        else:
            return "near_level"

# 创建智能预警管理器
alert_manager = SmartAlertManager(cooldown_minutes=15, breakthrough_threshold=0.2)

def run_alert_mode():
    log('[主流程] 进入关键点位预警模式...')
    prev_price = None
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, 'r') as f:
                prev_price = float(f.read().strip())
                log(f"[缓存] 读取到上次价格: {prev_price:,.2f}")
    except Exception as e:
        log(f"[警告] 读取缓存失败: {e}")

    current_price = get_btc_price()
    if current_price is None:
        log("[错误] 本次未能获取价格，跳过。")
        return

    if prev_price is None:
        log(f"[缓存] 无历史价格, 保存当前价 {current_price:,.2f} 后退出。")
        with open(CACHE_PATH, 'w') as f:
            f.write(str(current_price))
        return

    alert_reasons = []
    levels_to_check = {}
    
    # 1. 固定价格阈值 (仅在不使用MA200时生效)
    if not USE_MA200 and ALERT_PRICE:
        try:
            levels_to_check[f'固定阈值 ${float(ALERT_PRICE):,.0f}'] = float(ALERT_PRICE)
        except (ValueError, TypeError):
            log(f"[警告] 无效的 ALERT_PRICE 值: {ALERT_PRICE}")

    # 2. 整数关口
    for level in IMPORTANT_LEVELS:
        levels_to_check[f'整数关口 ${level:,}'] = level
        
    # 3. MA均线 (包含MA200, 如果启用)
    all_ma_days = sorted(list(set(MA_LEVELS + ([200] if USE_MA200 else []))))
    for days in all_ma_days:
        ma = get_btc_ma(days)
        if ma: levels_to_check[f'MA({days}) ${ma:,.0f}'] = ma

    for name, level in levels_to_check.items():
        can_alert, direction = alert_manager.can_alert(name, prev_price, current_price, level)
        if can_alert:
            if direction == "涨破":
                alert_reasons.append(f"🔴 🔺 涨破 {name}")
            elif direction == "跌破":
                alert_reasons.append(f"🟢 🔻 跌破 {name}")

    if alert_reasons:
        alert_msg = f"💰 现价: ${current_price:,.2f}\n\n" + "\n".join(alert_reasons)
        log(f"[预警] {alert_msg}")
        send_bark_alert(alert_msg)
    else:
        log(f"[主流程] 价格从 ${prev_price:,.2f} -> ${current_price:,.2f}, 未触发有效预警。")

    with open(CACHE_PATH, 'w') as f:
        f.write(str(current_price))

if __name__ == '__main__':
    if ALERT_MODE == 'report':
        run_report_mode()
    elif ALERT_MODE == 'alert':
        run_alert_mode()
    else:
        log(f"[错误] 未知的ALERT_MODE: {ALERT_MODE}") 