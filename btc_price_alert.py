import requests
import time
import os

# ====== 配置部分 ======
BARK_API_KEY = os.getenv('BARK_API_KEY', 'Znodd8yskndqUUbMVnmzBn,8ejJ8DaFxKFe7wd6m6sv7i') # 多个key用英文逗号分隔
BARK_API_KEYS = [k.strip() for k in BARK_API_KEY.split(',') if k.strip()]
BARK_API_URL_TEMPLATE = 'https://api.day.app/{}/'
ALERT_PRICE = os.getenv('ALERT_PRICE')
USE_MA200 = os.getenv('USE_MA200', 'false').lower() == 'true'
ALERT_MODE = os.getenv('ALERT_MODE', 'alert')  # 'alert' 或 'report'
CACHE_PATH = 'last_price.cache'

# ====== 关口与均线列表 ======
IMPORTANT_LEVELS = [
    100000, 105000, 110000, 115000, 120000, 125000, 130000, 135000, 140000, 145000, 150000
]
MA_LEVELS = [30, 90, 120]

print(f"[配置] ALERT_MODE: {ALERT_MODE}")
print(f"[配置] BARK_API_KEY: {'已设置' if BARK_API_KEY else '未设置'}")
print(f"[配置] USE_MA200: {USE_MA200}")
if ALERT_MODE == 'alert':
    print(f"[配置] ALERT_PRICE: {ALERT_PRICE}")

# ====== API获取函数 (与之前类似，保持不变) ======
def get_btc_price():
    apis = [
        ('CoinGecko', lambda: float(requests.get('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd', timeout=10).json()['bitcoin']['usd'])),
        ('OKX', lambda: float(requests.get('https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT', timeout=10).json()['data'][0]['last']))
    ]
    for name, func in apis:
        try:
            price = func()
            print(f"[数据] 通过 {name} 获取到价格: {price}")
            return price
        except Exception as e:
            print(f"[错误] {name} 获取价格失败: {e}")
    return None

def get_btc_ma(days):
    try:
        url = f'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        prices = [item[1] for item in data['prices']]
        if len(prices) > days: # API可能返回多一天的数据
            prices = prices[-days:]
        return sum(prices) / len(prices)
    except Exception as e:
        print(f"[错误] CoinGecko 获取 MA({days}) 失败: {e}")
        return None

def get_btc_24h_change():
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true'
        resp = requests.get(url, timeout=10)
        return float(resp.json()['bitcoin']['usd_24h_change'])
    except Exception as e:
        print(f"[错误] CoinGecko 获取24h涨跌幅失败: {e}")
        return None

# ====== 发送推送 ======
def send_bark_alert(body_msg):
    title = 'BTC行情提醒'
    for key in BARK_API_KEYS:
        url = f"{BARK_API_URL_TEMPLATE.format(key)}{title}/{body_msg}"
        print(f"[推送] 发送Bark通知: {url.replace(key, '***')}")
        try:
            requests.get(url, timeout=10)
            print('[推送] 通知已发送')
        except Exception as e:
            print(f'[错误] 推送失败: {e}')

# ====== 运行模式 ======
def run_report_mode():
    print('[主流程] 进入定时报告模式...')
    price = get_btc_price()
    if price is None:
        send_bark_alert("获取BTC行情失败")
        return

    change = get_btc_24h_change()
    report_msg = f"现价:${price:,.2f} | 24h涨跌:{change:+.2f}%"

    all_ma_days = sorted(list(set(MA_LEVELS + ([200] if USE_MA200 else []))))
    for days in all_ma_days:
        ma = get_btc_ma(days)
        if ma:
            report_msg += f" | MA({days}):{ma:,.0f}"
    
    print(f"[报告内容] {report_msg}")
    send_bark_alert(report_msg)

def run_alert_mode():
    print('[主流程] 进入关键点位预警模式...')
    prev_price = None
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, 'r') as f:
                prev_price = float(f.read().strip())
                print(f"[缓存] 读取到上次价格: {prev_price:,.2f}")
    except Exception as e:
        print(f"[警告] 读取缓存失败: {e}")

    current_price = get_btc_price()
    if current_price is None:
        print("[错误] 本次未能获取价格，跳过。")
        return

    if prev_price is None:
        print(f"[缓存] 无历史价格, 保存当前价 {current_price:,.2f} 后退出。")
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
            print(f"[警告] 无效的 ALERT_PRICE 值: {ALERT_PRICE}")

    # 2. 整数关口
    for level in IMPORTANT_LEVELS:
        levels_to_check[f'整数关口 ${level:,}'] = level
        
    # 3. MA均线 (包含MA200, 如果启用)
    all_ma_days = sorted(list(set(MA_LEVELS + ([200] if USE_MA200 else []))))
    for days in all_ma_days:
        ma = get_btc_ma(days)
        if ma: levels_to_check[f'MA({days}) ${ma:,.0f}'] = ma

    for name, level in levels_to_check.items():
        if prev_price < level and current_price > level:
            alert_reasons.append(f"涨破 {name}")
        if prev_price > level and current_price < level:
            alert_reasons.append(f"跌破 {name}")

    if alert_reasons:
        alert_msg = f"现价:${current_price:,.2f} | " + " & ".join(alert_reasons)
        print(f"[预警] {alert_msg}")
        send_bark_alert(alert_msg)
    else:
        print(f"[主流程] 价格从 ${prev_price:,.2f} -> ${current_price:,.2f}, 未穿越关键点位。")

    with open(CACHE_PATH, 'w') as f:
        f.write(str(current_price))

if __name__ == '__main__':
    if ALERT_MODE == 'report':
        run_report_mode()
    elif ALERT_MODE == 'alert':
        run_alert_mode()
    else:
        print(f"[错误] 未知的ALERT_MODE: {ALERT_MODE}") 