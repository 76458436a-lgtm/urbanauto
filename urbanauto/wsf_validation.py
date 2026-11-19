"""Pure helpers for validating WSF categorical rasters and STAC responses."""
import re

def parse_built_codes(value):
    codes = [s.strip() for s in value.split(',')]
    if not codes or any(not re.fullmatch(r'\d+', s) for s in codes):
        raise ValueError('Укажите целочисленные коды застройки через запятую, например 1')
    nums = [int(s) for s in codes]
    if len(set(nums)) != len(nums) or any(x > 65535 for x in nums):
        raise ValueError('Повторяющиеся или недопустимые коды')
    return nums

def validate_coverage(valid_pixels, total_pixels):
    if total_pixels <= 0 or valid_pixels != total_pixels:
        raise ValueError('WSF не покрывает всю область: %s из %s пикселей' % (valid_pixels, total_pixels))

def stac_next(link, current_url, current_payload):
    from urllib.parse import urljoin
    method = link.get('method', 'GET').upper()
    if method not in ('GET', 'POST'):
        raise ValueError('Неизвестный метод STAC next')
    body = link.get('body') if method == 'POST' else None
    if method == 'POST' and body is None:
        body = current_payload
    return urljoin(current_url, link['href']), body
