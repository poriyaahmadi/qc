# -*- coding: utf-8 -*-
"""
گرفتن اطلاعات محصول از دیجی‌کالا با curl خام (برای عبور از محدودیت‌های احتمالی).
این ماژول مستقل است تا هم مسیر تکی و هم مسیر batch بتوانند از آن استفاده کنند.
"""

import subprocess
import json


class ProductNotFoundError(Exception):
    pass


def get_product_info(dkp: str) -> dict:
    url = f"https://api.digikala.com/v2/product/{dkp}/"
    result = subprocess.run([
        'curl', '-s', '-L', '--max-redirs', '10',
        '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        '-H', 'Accept: application/json, text/plain, */*',
        '-H', 'Accept-Language: fa,en;q=0.9',
        '-H', 'Referer: https://www.digikala.com/',
        '-H', 'Origin: https://www.digikala.com',
        '--compressed', url
    ], capture_output=True, text=True, timeout=20)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ProductNotFoundError(f"پاسخ دیجی‌کالا برای DKP-{dkp} قابل خواندن نبود.")

    product = data.get('data', {}).get('product', {})
    if not product:
        raise ProductNotFoundError(f"محصول با کد DKP-{dkp} پیدا نشد.")

    title_en = product.get('title_en', '') or product.get('test_title_en', '')
    title_fa = product.get('title_fa', '') or product.get('title', '')
    data_layer = product.get('data_layer', {})
    category_raw = data_layer.get('category', '')
    category = category_raw.replace('[', '').replace(']', '').split(',')[-1].strip() if category_raw else ''
    brand = data_layer.get('brand', '')
    images = product.get('images', {})
    main_image = ''
    if images.get('main', {}).get('url'):
        urls = images['main']['url']
        main_image = urls[0] if isinstance(urls, list) else urls

    specifications = []
    for group in product.get('specifications', []) or []:
        for attr in group.get('attributes', []) or []:
            title = attr.get('title', '')
            values = attr.get('values', [])
            if title and values:
                specifications.append(f"{title}: {', '.join(values)}")

    return {
        'dkp': dkp,
        'title_en': title_en,
        'title_fa': title_fa,
        'image': main_image,
        'category': category,
        'brand': brand,
        'specifications': specifications[:25],
    }
