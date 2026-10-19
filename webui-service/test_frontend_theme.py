"""issue #72 前端结构/配色/布局契约；动态主题行为由 test_app_js.js 覆盖。"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(encoding="utf-8"), flags=re.S)


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def elements():
    parsed = Elements()
    parsed.feed(HTML)
    return parsed.tags


def blocks(selector):
    return re.findall(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}", CSS)


def test_theme_boot_is_synchronous_before_stylesheet():
    tags = elements()
    boot = next(i for i, (tag, attrs) in enumerate(tags)
                if tag == "script" and attrs.get("src", "").endswith("/theme-boot.js"))
    style = next(i for i, (tag, attrs) in enumerate(tags)
                 if tag == "link" and attrs.get("rel") == "stylesheet")
    app = next(i for i, (tag, attrs) in enumerate(tags)
               if tag == "script" and attrs.get("src", "").endswith("/app.js"))
    assert boot < style < app
    assert not {"async", "defer", "type"}.intersection(tags[boot][1])
    for _, attrs in tags:
        asset = attrs.get("src", attrs.get("href", ""))
        if asset.startswith("/app/fnmusic-ext/static/"):
            assert (STATIC / asset.rsplit("/", 1)[1]).is_file(), asset


def test_settings_has_desktop_mobile_navigation_and_three_local_preferences():
    tags = elements()
    assert len([a for tag, a in tags if tag == "button" and a.get("data-page") == "settings"]) == 2
    assert len([a for tag, a in tags if tag == "section" and a.get("id") == "page-settings"]) == 1
    radios = [a for tag, a in tags if tag == "input" and a.get("name") == "theme"]
    assert [a["value"] for a in radios] == ["auto", "light", "dark"]
    assert all(a.get("type") == "radio" for a in radios)
    # 三方合并必须保留 dev 新增的推荐参数与 maintenance 弹窗/进度区。
    ids = {a["id"] for _, a in tags if "id" in a}
    assert {"llm-api-warning", "llm-liked-settings", "liked-refresh-hours", "liked-size",
            "cleanup-modal", "cleanup-confirm-btn", "cleanup-status", "cleanup-details"} <= ids


def test_theme_tokens_cover_surfaces_and_keep_code_images_white():
    light = blocks(":root")[0]
    dark = blocks(':root[data-theme="dark"]')[0]
    tokens = lambda body: set(re.findall(r"(--[\w-]+)\s*:", body))
    assert "color-scheme: light" in light
    assert "color-scheme: dark" in dark
    colors = {"--bg", "--card", "--line", "--text", "--muted", "--accent", "--accent-soft",
              "--input-bg", "--chip-bg", "--surface-2", "--badge-dim-bg", "--ok-soft",
              "--dot-idle", "--overlay", "--bar-bg", "--nav-bg", "--toast-bg", "--toast-text",
              "--focus-ring", "--shadow", "--shadow-up", "--ok", "--warn", "--err"}
    assert colors <= tokens(light) & tokens(dark)
    assert set(re.findall(r"var\((--[\w-]+)\)", CSS)) <= tokens(light)
    for selector in ["#qr-img", ".about-donate-img"]:
        assert "background: #fff" in blocks(selector)[0], "二维码不能随主题反色"
    for selector in [".input", ".chip", ".report", ".modal-mask", ".toast", ".bottom-nav"]:
        body = " ".join(blocks(selector))
        assert "background: var(--" in body, selector


@pytest.mark.parametrize("selector", [":root", ':root[data-theme="dark"]'])
def test_solid_controls_and_status_toasts_have_readable_contrast(selector):
    values = dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", blocks(selector)[0]))

    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                  for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    for control, background in [(".nav-item.active", "--accent"),
                                (".btn.primary", "--accent"),
                                (".btn.danger:hover", "--err"),
                                (".badge.new", "--err"),
                                (".toast.ok", "--ok"), (".toast.fail", "--err")]:
        body = blocks(control)[0]
        assert f"background: var({background})" in body
        assert "color: var(--on-accent)" in body
        low, high = sorted((luminance(values[background]), luminance(values["--on-accent"])))
        assert (high + 0.05) / (low + 0.05) >= 4.5, (selector, control)


def test_pages_share_centered_content_width_and_save_bar_alignment():
    main = blocks(".main")
    assert len(main) == 2  # 桌面主规则 + 手机内边距覆盖，共用同一个宽度上限。
    assert "max-width: var(--content-max)" in main[0]
    assert "margin: 0 auto" in main[0]
    assert "width: 100%" in blocks(".page")[0]
    about = blocks(".about-wrap")[0]
    assert "width: 100%" in about and "max-width" not in about
    save_bar = blocks(".save-bar")[0]
    assert "--content-max" in save_bar and "--sidebar-w" in save_bar
    assert "padding-left: max(" in save_bar and "padding-right: max(" in save_bar
    mobile = CSS.split("@media (max-width: 760px)", 1)[1]
    assert re.search(r"\.main\s*\{[^}]*14px", mobile)
    assert re.search(r"\.save-bar\s*\{[^}]*padding: 10px 14px", mobile)
    assert "safe-area-inset-bottom" in mobile


@pytest.mark.parametrize("filename", ["theme-boot.js", "app.js"])
def test_theme_preference_key_matches_boot_and_app(filename):
    assert '"fnmusic-theme"' in (STATIC / filename).read_text(encoding="utf-8")
