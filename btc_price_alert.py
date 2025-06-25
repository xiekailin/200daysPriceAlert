import requests
import time
import os

# ====== 配置部分 ======
BARK_API_KEY = os.getenv('BARK_API_KEY', 'Znodd8yskndqUUbMVnmzBn')  # 多个key用英文逗号分隔
BARK_API_KEYS = [k.strip() for k in BARK_API_KEY.split(',') if k.strip()]
BARK_API_URL_TEMPLATE = 'https://api.day.app/{}/'
ALERT_PRICE = os.getenv('ALERT_PRICE')  # 可以设置为具体价格
USE_MA200 = os.getenv('USE_MA200', 'false').lower() == 'true'  # 是否用200日均线
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '2'))  # 检查频率（秒），默认2秒
ALERT_DIRECTION = os.getenv('ALERT_DIRECTION', 'down')  # up/down/both
PERCENT_ALERT = os.getenv('PERCENT_ALERT', 'false').lower() == 'true'  # 是否启用百分比预警
PERCENT_THRESHOLD = float(os.getenv('PERCENT_THRESHOLD', '5'))  # 百分比阈值，默认5%

print(f"[配置] BARK_API_KEY: {'已设置' if BARK_API_KEY else '未设置'}")
print(f"[配置] ALERT_PRICE: {ALERT_PRICE}")
print(f"[配置] USE_MA200: {USE_MA200}")
print(f"[配置] CHECK_INTERVAL: {CHECK_INTERVAL}")
print(f"[配置] ALERT_DIRECTION: {ALERT_DIRECTION}")
print(f"[配置] PERCENT_ALERT: {PERCENT_ALERT}")
print(f"[配置] PERCENT_THRESHOLD: {PERCENT_THRESHOLD}")

# ====== 整数关口列表 ======
IMPORTANT_LEVELS = [
    100000, 105000, 110000, 115000, 120000, 125000, 130000, 135000, 140000, 145000, 150000
]

# ====== 均线列表 ======
MA_LEVELS = [30, 90, 120]

# ====== 多API获取BTC现价 ======
def get_btc_price():
    apis = [
        ('CoinGecko', lambda: requests.get('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd', timeout=5).json()['bitcoin']['usd']),
        ('OKX', lambda: float(requests.get('https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT', timeout=5).json()['data'][0]['last'])),
        ('Coinbase', lambda: float(requests.get('https://api.coinbase.com/v2/prices/spot?currency=USD', timeout=5).json()['data']['amount'])),
        ('Bitstamp', lambda: float(requests.get('https://www.bitstamp.net/api/v2/ticker/btcusd/', timeout=5).json()['last'])),
        ('Kraken', lambda: float(requests.get('https://api.kraken.com/0/public/Ticker?pair=XBTUSD', timeout=5).json()['result']['XXBTZUSD']['c'][0]))
    ]
    for name, func in apis:
        try:
            print(f"[请求] 尝试{name}获取BTC现价...")
            price = func()
            print(f"[结果] {name} BTC价格: {price}")
            return float(price)
        except Exception as e:
            print(f"[错误] {name}获取失败: {e}")
    print('[错误] 所有API获取BTC现价均失败')
    return None

# ====== 多API获取BTC均线 ======
def get_btc_ma(days):
    # CoinGecko
    try:
        print(f"[请求] CoinGecko获取BTC {days}日均线...")
        url = f'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=daily'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [item[1] for item in data['prices']]
        if len(closes) > days:
            closes = closes[-days:]
        ma = sum(closes) / len(closes)
        print(f"[结果] CoinGecko {days}日均线: {ma:.2f}")
        return ma
    except Exception as e:
        print(f"[错误] CoinGecko获取{days}日均线失败: {e}")
    # OKX
    try:
        print(f"[请求] OKX获取BTC {days}日均线...")
        url = f'https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1D&limit={days}'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item[4]) for item in data['data']]
        ma = sum(closes) / len(closes)
        print(f"[结果] OKX {days}日均线: {ma:.2f}")
        return ma
    except Exception as e:
        print(f"[错误] OKX获取{days}日均线失败: {e}")
    # Bitstamp
    try:
        print(f"[请求] Bitstamp获取BTC {days}日均线...")
        url = f'https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit={days}'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item['close']) for item in data['data']['ohlc']]
        ma = sum(closes) / len(closes)
        print(f"[结果] Bitstamp {days}日均线: {ma:.2f}")
        return ma
    except Exception as e:
        print(f"[错误] Bitstamp获取{days}日均线失败: {e}")
    # Kraken
    try:
        print(f"[请求] Kraken获取BTC {days}日均线...")
        url = 'https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440&since=0'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item[4]) for item in list(data['result'].values())[0] if isinstance(item, list)]
        closes = closes[-days:]
        ma = sum(closes) / len(closes)
        print(f"[结果] Kraken {days}日均线: {ma:.2f}")
        return ma
    except Exception as e:
        print(f"[错误] Kraken获取{days}日均线失败: {e}")
    print(f'[错误] 所有API获取{days}日均线均失败')
    return None

# ====== 获取24小时涨跌幅百分比 ======
def get_btc_24h_change():
    # 优先用CoinGecko
    try:
        print("[请求] CoinGecko获取24小时涨跌幅...")
        url = 'https://api.coingecko.com/api/v3/coins/bitcoin?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        percent = float(data['market_data']['price_change_percentage_24h'])
        print(f"[结果] CoinGecko 24h涨跌幅: {percent}%")
        return percent
    except Exception as e:
        print(f"[错误] CoinGecko获取24h涨跌幅失败: {e}")
    # OKX
    try:
        print("[请求] OKX获取24小时涨跌幅...")
        url = 'https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        percent = float(data['data'][0]['change24h']) / float(data['data'][0]['open24h']) * 100
        print(f"[结果] OKX 24h涨跌幅: {percent}%")
        return percent
    except Exception as e:
        print(f"[错误] OKX获取24h涨跌幅失败: {e}")
    print('[错误] 所有API获取24小时涨跌幅均失败')
    return None

# ====== 发送Bark推送 ======
def send_bark_alert(body_msg):
    title = 'BTC价格预警'
    body = body_msg
    for key in BARK_API_KEYS:
        url = f"{BARK_API_URL_TEMPLATE.format(key)}{title}/{body}"
        print(f"[推送] 发送Bark通知: {url.replace(key, '***')}")
        try:
            resp = requests.get(url)
            print(f'[推送] 已发送推送通知，返回状态码: {resp.status_code}')
        except Exception as e:
            print(f'[错误] 推送失败: {e}')

# ====== 主循环 ======
if __name__ == '__main__':
    if USE_MA200:
        threshold = get_btc_ma(200)
        print(f'[主流程] 使用200日均线作为主要阈值: {threshold}')
    else:
        threshold = float(ALERT_PRICE) if ALERT_PRICE else 60000
        print(f'[主流程] 使用固定阈值: {threshold}')
    
    # Calculate important MAs
    ma_values = {}
    print('[主流程] 开始计算需要监控的均线...')
    for days in MA_LEVELS:
        ma_value = get_btc_ma(days)
        if ma_value:
            ma_values[days] = ma_value
            print(f'[主流程] MA{days}: {ma_value:.2f}')

    while True:
        price = get_btc_price()
        alert_reasons = []
        if price is not None:
            # Threshold check
            if ALERT_DIRECTION in ['down', 'both'] and price < threshold:
                alert_reasons.append(f'已跌破阈值${threshold:.2f}')
            if ALERT_DIRECTION in ['up', 'both'] and price > threshold:
                alert_reasons.append(f'已涨破阈值${threshold:.2f}')

            # Percent change check
            if PERCENT_ALERT:
                percent = get_btc_24h_change()
                if percent is not None and abs(percent) >= PERCENT_THRESHOLD:
                    alert_reasons.append(f'24h涨跌幅: {percent:.2f}%')

            # Integer level check
            for level in IMPORTANT_LEVELS:
                if abs(price - level) < 100:  # 离关口100美元内提醒
                    alert_reasons.append(f'接近整数关口:${level}')
            
            # MA level check
            for days, ma_value in ma_values.items():
                if abs(price - ma_value) < 100: # 价格距离均线100美元以内
                    alert_reasons.append(f'接近MA({days})均线:${ma_value:.2f}')
            
            if alert_reasons:
                alert_msg = f'BTC当前价: ${price:.2f} | ' + ' | '.join(alert_reasons)
                print(f'[主流程] 触发预警，准备推送: {alert_msg}')
                send_bark_alert(alert_msg)
                break
            else:
                print(f'[主流程] 当前BTC价格: ${price:.2f}，未触发任何预警，无需推送。')
        else:
            print('[主流程] 获取价格失败，等待重试...')
        time.sleep(CHECK_INTERVAL) 