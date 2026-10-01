"""A2A 出站脱敏的单元测试（黑名单口径）。"""

from lquant.agent.redact import MASK, PATH_MASK, is_sensitive_key, redact, redact_text


class TestSensitiveKey:
    def test_blacklist_hits(self):
        for key in ("token", "auth_token", "access_token", "api_key", "apiKey",
                    "API-KEY", "secret", "client_secret", "password", "passwd",
                    "passphrase", "credential", "private_key", "access_key",
                    "authorization", "cookie"):
            assert is_sensitive_key(key), key

    def test_normal_keys_not_hit(self):
        # 裸 "auth" 刻意不入黑名单：否则 author/authority 会被误伤
        for key in ("author", "authority", "symbol", "start_date", "end_date",
                    "fields", "limit", "permissionMode", "model", "tools"):
            assert not is_sensitive_key(key), key


class TestRedactStructure:
    def test_sensitive_key_masks_whole_subtree(self):
        got = redact({"auth_token": {"nested": "sk-should-not-leak"}, "symbol": "600519"})
        assert got == {"auth_token": MASK, "symbol": "600519"}

    def test_non_sensitive_nested_dict_kept(self):
        got = redact({"args": {"symbols": ["600519"], "limit": 5}})
        assert got == {"args": {"symbols": ["600519"], "limit": 5}}

    def test_lists_recursed(self):
        got = redact([{"token": "x"}, {"symbol": "600519"}])
        assert got == [{"token": MASK}, {"symbol": "600519"}]

    def test_non_string_scalars_passthrough(self):
        assert redact({"a": 1, "b": True, "c": None, "d": 1.5}) == {
            "a": 1, "b": True, "c": None, "d": 1.5}

    def test_tuple_recursed_to_list(self):
        assert redact(("sk-abcdefgh", 1)) == [MASK, 1]


class TestRedactText:
    def test_empty_passthrough(self):
        assert redact_text("") == ""

    def test_credential_shapes(self):
        cases = [
            "key=sk-abcdefghijklmn",
            "ghp_" + "a" * 24,
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmn",
            "Authorization: Bearer abcdefghijklmn",
            "xoxb-1234567890-abcdef",
        ]
        for raw in cases:
            assert MASK in redact_text(raw), raw

    def test_local_absolute_paths_masked(self):
        for raw in ("/Users/lyp/code/lquant/data.parquet",
                    "/home/quant/secret.txt",
                    "/var/log/lquant.log",
                    "/tmp/pytest-of-root/x",
                    "/private/var/folders/zz"):
            assert redact_text(raw) == PATH_MASK, raw

    def test_relative_paths_and_urls_untouched(self):
        for raw in ("data/parquet/daily/year=2026/part-0.parquet",
                    "https://api.example.com/v1/quotes?symbol=600519",
                    "600519 最新收盘 1702.5 元"):
            assert redact_text(raw) == raw, raw

    def test_plain_chinese_text_untouched(self):
        raw = "贵州茅台 600519 最新收盘 1702.5 元，PE(TTM) 22.1。"
        assert redact_text(raw) == raw


class TestRedactMixed:
    def test_tool_args_with_path_and_token(self):
        got = redact({"script": "/Users/lyp/x.py", "auth_token": "sk-abcdefghijkl"})
        assert got == {"script": PATH_MASK, "auth_token": MASK}

    def test_deep_nesting(self):
        got = redact({"a": {"b": {"c": {"password": "p", "keep": "v"}}}})
        assert got == {"a": {"b": {"c": {"password": MASK, "keep": "v"}}}}
