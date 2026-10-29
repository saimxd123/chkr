import os

BOT_TOKEN       = os.getenv("BOT_TOKEN", "8558886316:AAG3qGKnqX_o2nBb0NVRlM4E8B4oxRpCzKo")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "8919487892"))
DEFAULT_PIN     = os.getenv("DEFAULT_PIN", "OGGY-PROO")

DB_PATH         = os.getenv("DB_PATH", "/data/oggy.db")
PORT            = int(os.getenv("PORT", "10000"))

CONCURRENCY     = int(os.getenv("CONCURRENCY", "5"))
NAV_TIMEOUT_MS  = int(os.getenv("NAV_TIMEOUT_MS", "25000"))
HTTP_TIMEOUT    = int(os.getenv("HTTP_TIMEOUT", "20"))
MAX_URLS        = int(os.getenv("MAX_URLS", "20000"))
DEFAULT_MAX_PRICE = float(os.getenv("DEFAULT_MAX_PRICE", "5"))

SHOPIFY_SIGS = [
    "cdn.shopify.com", "shopify.theme", "myshopify.com",
    "shopify-features", "Shopify.theme",
]
CAPTCHA_SIGS = [
    "hcaptcha.com", "recaptcha/api.js", "google.com/recaptcha",
    "challenges.cloudflare.com", "cf-turnstile", "g-recaptcha",
    "h-captcha", "shopify-payment-terms-captcha",
]
CART_PATHS = ["/cart", "/cart.js", "/checkout", "/collections/all"]
