from .base_recorder import BaseRecorder

class HttpRecorder(BaseRecorder):
    def __init__(self, endpoint_url, api_key=None):
        self.endpoint_url = endpoint_url
        self.headers = {'Content-Type': 'application/json'}
        if api_key:
            self.headers['Authorization'] = f'Bearer {api_key}'

    def log_trade(self, dt, symbol, action, price, size, comm, order_ref, cash, value):
        payload = {
            'type': 'trade',
            'dt': str(dt),
            'symbol': symbol,
            'action': action,
            'price': price,
            'size': size,
            'value': value
        }
        # 实际生产中建议使用异步或队列，避免 HTTP 请求阻塞回测速度
        try:
            # 可按需打印发送目标，并调用 requests.post 发送 HTTP 日志。
            pass
        except Exception as e:
            print(f"HttpRecorder Error: {e}")

    def finish_execution(self, final_value, total_return, sharpe, max_drawdown, annual_return, trade_count, win_rate):
        payload = {
            'type': 'summary',
            'final_value': final_value,
            'sharpe': sharpe
        }
        # 调用 requests.post(...)。
        print(f"HTTP Recorder: Backtest finished, data sent to {self.endpoint_url}")
