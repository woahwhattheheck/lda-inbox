from __future__ import annotations

import unittest

from effect_receipt_common import EffectError, load_json_bytes


class EffectJsonLimitTests(unittest.TestCase):
    def test_integer_digit_limit_fails_closed_as_effect_error(self) -> None:
        payload = b'{"value":' + (b'9' * 5000) + b'}'
        with self.assertRaisesRegex(EffectError, '^INVALID_JSON$'):
            load_json_bytes(payload)

    def test_loader_preserves_specific_duplicate_key_error(self) -> None:
        with self.assertRaisesRegex(EffectError, '^DUPLICATE_JSON_KEY:value$'):
            load_json_bytes(b'{"value":1,"value":2}')

    def test_loader_preserves_specific_nonfinite_constant_error(self) -> None:
        with self.assertRaisesRegex(EffectError, '^NONFINITE_JSON:NaN$'):
            load_json_bytes(b'{"value":NaN}')


if __name__ == '__main__':
    unittest.main()
