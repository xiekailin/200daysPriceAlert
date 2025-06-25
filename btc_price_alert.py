import requests
import time
import os

# ====== 配置部分 ======
BARK_API_KEY = os.getenv('BARK_API_KEY', 'Znodd8yskndqUUbMVnmzBn')  # 建议用GitHub Secrets设置
BARK_API_URL = f'https://api.day.app/{BARK_API_KEY}/'
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

# ====== 多API获取BTC 200日均线 ======
def get_btc_ma200():
    # CoinGecko
    try:
        print("[请求] CoinGecko获取BTC 200日均线...")
        url = 'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=200&interval=daily'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [item[1] for item in data['prices']]
        ma200 = sum(closes) / len(closes)
        print(f"[结果] CoinGecko 200日均线: {ma200}")
        return ma200
    except Exception as e:
        print(f"[错误] CoinGecko获取200日均线失败: {e}")
    # OKX
    try:
        print("[请求] OKX获取BTC 200日均线...")
        url = 'https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1D&limit=200'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item[4]) for item in data['data']]
        ma200 = sum(closes) / len(closes)
        print(f"[结果] OKX 200日均线: {ma200}")
        return ma200
    except Exception as e:
        print(f"[错误] OKX获取200日均线失败: {e}")
    # Coinbase
    try:
        print("[请求] Coinbase获取BTC 200日均线...")
        closes = []
        for i in range(200):
            # Coinbase没有历史K线API，只能跳过
            raise Exception('Coinbase不支持200日均线')
        # 这里不会执行
    except Exception as e:
        print(f"[错误] Coinbase获取200日均线失败: {e}")
    # Bitstamp
    try:
        print("[请求] Bitstamp获取BTC 200日均线...")
        url = 'https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=200'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item['close']) for item in data['data']['ohlc']]
        ma200 = sum(closes) / len(closes)
        print(f"[结果] Bitstamp 200日均线: {ma200}")
        return ma200
    except Exception as e:
        print(f"[错误] Bitstamp获取200日均线失败: {e}")
    # Kraken
    try:
        print("[请求] Kraken获取BTC 200日均线...")
        url = 'https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440&since=0'
        resp = requests.get(url, timeout=10)
        data = resp.json()
        closes = [float(item[4]) for item in list(data['result'].values())[0] if isinstance(item, list)]
        closes = closes[-200:]
        ma200 = sum(closes) / len(closes)
        print(f"[结果] Kraken 200日均线: {ma200}")
        return ma200
    except Exception as e:
        print(f"[错误] Kraken获取200日均线失败: {e}")
    print('[错误] 所有API获取200日均线均失败')
    return None

# ====== 发送Bark推送 ======
def send_bark_alert(price, threshold):
    title = 'BTC价格预警'
    body = f'BTC价格${price:.2f}已跌破了阈值${threshold:.2f}'
    url = f"{BARK_API_URL}{title}/{body}"
    print(f"[推送] 发送Bark通知: {url}")
    try:
        resp = requests.get(url)
        print(f'[推送] 已发送推送通知，返回状态码: {resp.status_code}')
    except Exception as e:
        print(f'[错误] 推送失败: {e}')

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

# ====== 主循环 ======
if __name__ == '__main__':
    if USE_MA200:
        threshold = get_btc_ma200()
        print(f'[主流程] 使用200日均线作为阈值: {threshold}')
    else:
        threshold = float(ALERT_PRICE) if ALERT_PRICE else 60000
        print(f'[主流程] 使用固定阈值: {threshold}')
    while True:
        price = get_btc_price()
        alert_triggered = False
        alert_msg = ''
        if price is not None:
            print(f'[主流程] 当前BTC价格：{price}，阈值：{threshold}')
            # 双向预警
            if ALERT_DIRECTION in ['down', 'both'] and price < threshold:
                alert_triggered = True
                alert_msg = f'BTC价格${price:.2f}已跌破阈值${threshold:.2f}'
            if ALERT_DIRECTION in ['up', 'both'] and price > threshold:
                alert_triggered = True
                alert_msg = f'BTC价格${price:.2f}已涨破阈值${threshold:.2f}'
            # 百分比涨跌幅预警
            if PERCENT_ALERT:
                percent = get_btc_24h_change()
                if percent is not None and abs(percent) >= PERCENT_THRESHOLD:
                    alert_triggered = True
                    alert_msg += f' | 24小时涨跌幅：{percent:.2f}%'
            if alert_triggered:
                print(f'[主流程] 触发预警，准备推送...')
                send_bark_alert(price, threshold)
                break  # 只推送一次，推送后退出。如需持续推送可去掉这行
            else:
                print(f'[主流程] 未触发任何预警，无需推送。')
        else:
            print('[主流程] 获取价格失败，等待重试...')
        time.sleep(CHECK_INTERVAL) 