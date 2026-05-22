import yaml
from pathlib import Path
from typing import Dict, Any, Optional

# 配置类
class Config: 
    def __init__(self, config_path: str = "config.yml"):
        self.config_path = Path(config_path)
        try:
            self.config_data = self._load_config()
        except FileNotFoundError:
            raise FileNotFoundError(f"{self.config_path} 配置文件不存在！")
        except yaml.YAMLError:
            raise yaml.YAMLError(f"{self.config_path} 配置文件内容语法错误！")
        pass

    # 加载配置文件    
    def _load_config(self) -> Dict[str, Any]:
        with open(self.config_path, 'r', encoding="utf-8") as f:
            return yaml.safe_load(f)
    
    @property
    def api_id(self) -> Optional[int]:
        return self.config_data["telegram"]["api_id"]
    
    @property
    def api_hash(self) -> Optional[str]:
        return self.config_data["telegram"]["api_hash"]
    
    @property
    def bot_token(self) -> Optional[str]:
        return self.config_data["telegram"]["bot_token"]
    
    @property
    def exclusive_bot_id(self) -> Optional[str]:
        return self.config_data["telegram"]["exclusive_bot_id"]
    
    @property
    def exclusive_user_id(self) -> Optional[int]:
        return self.config_data["telegram"]["exclusive_user_id"]
    
    @property
    def proxy_enabled(self) -> Optional[bool]:
        return self.config_data["telegram"].get("proxy", {}).get("enabled", False)
    
    @property
    def proxy_config(self) -> Optional[Dict[str, Any]]:
        proxy_settings = self.config_data["telegram"].get("proxy", {})
        if proxy_settings.get("enabled"):
            return {
                'proxy_type': proxy_settings['proxy_type'],
                'addr': proxy_settings['addr'],
                'port': proxy_settings['port'],
                'username': proxy_settings.get('username'),
                'password': proxy_settings.get('password')
            }
        return None
    
    @property
    def admin_id(self) -> Optional[int]:
        """获取管理员ID，用于接收支付通知"""
        return self.config_data.get("vip", {}).get("admin_id")
    
    @property
    def huobi_enabled(self) -> bool:
        """火币API是否启用"""
        return self.config_data.get("payment", {}).get("huobi", {}).get("enabled", False)
    
    @property
    def huobi_access_key(self) -> Optional[str]:
        """获取火币API访问密钥"""
        return self.config_data.get("payment", {}).get("huobi", {}).get("access_key")
    
    @property
    def huobi_secret_key(self) -> Optional[str]:
        """获取火币API密钥"""
        return self.config_data.get("payment", {}).get("huobi", {}).get("secret_key")
    
    @property
    def wechat_pay_url(self) -> Optional[str]:
        """获取微信支付链接"""
        return self.config_data.get("payment", {}).get("wechat")
    
    @property
    def alipay_url(self) -> Optional[str]:
        """获取支付宝支付链接"""
        return self.config_data.get("payment", {}).get("alipay")
    
    @property
    def vip_group_link(self) -> Optional[str]:
        """获取VIP群链接"""
        return self.config_data.get("vip", {}).get("group_link")
    
    @property
    def vip_group_id(self) -> Optional[str]:
        """获取VIP群ID"""
        return self.config_data.get("vip", {}).get("group_id")
    
    @property
    def tutorial_link(self) -> Optional[str]:
        """获取USDT使用教程链接"""
        return self.config_data.get("vip", {}).get("tutorial_link")
    
    def get_price(self, membership_type: str, currency: str) -> int:
        """
        获取会员价格
        
        Args:
            membership_type: 会员类型 (yearly/lifetime)
            currency: 货币类型 (rmb/usdt)
            
        Returns:
            价格值
        """
        default_prices = {
            'yearly': {'rmb': 255, 'usdt': 36},
            'lifetime': {'rmb': 388, 'usdt': 54}
        }
        
        return self.config_data.get("payment", {}).get("prices", {})\
            .get(membership_type, {})\
            .get(currency, default_prices[membership_type][currency])
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项值，支持点号分隔的路径，如'payment.huobi.enabled'"""
        try:
            parts = key.split('.')
            value = self.config_data
            for part in parts:
                value = value.get(part, {})
            return value if value != {} else default
        except:
            return default
        
