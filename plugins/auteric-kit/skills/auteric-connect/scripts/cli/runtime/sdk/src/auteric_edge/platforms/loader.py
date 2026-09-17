"""Local-only native adapter configuration. Secret values come from environment."""

import os


def load_platform(config):
    platform = config["platform"]
    if platform == "shopify":
        from .shopify import ShopifyConnector

        return ShopifyConnector(
            config["shop"],
            os.environ[config["token_env"]],
            database=config["database"],
            currency=config.get("currency", "USD"),
            token_type=config.get("token_type", "public"),
            checkout_hosts=config.get("checkout_hosts"),
        )
    if platform == "bigcommerce":
        from .bigcommerce import BigCommerceConnector

        return BigCommerceConnector(
            store_hash=config["store_hash"],
            access_token=os.environ[config["token_env"]],
            currency=config["currency"],
            channel_id=config.get("channel_id", 1),
            state_path=config["database"],
            checkout_hosts=config["checkout_hosts"],
        )
    if platform == "woocommerce":
        from .woocommerce import WooCommerceConnector

        return WooCommerceConnector(config["base_url"], database=config["database"])
    if platform == "adobe":
        from .adobe import AdobeCommerceConnector

        return AdobeCommerceConnector(
            config["base_url"],
            database=config["database"],
            store_view=config.get("store_view", "default"),
            currency=config["currency"],
        )
    if platform == "wix":
        from .wix import WixConnector

        return WixConnector(
            site_id=config["site_id"],
            api_key=os.environ[config["token_env"]],
            currency=config["currency"],
            database=config["database"],
            checkout_hosts=config["checkout_hosts"],
        )
    raise ValueError("Unsupported native platform; no generic fallback will execute")
