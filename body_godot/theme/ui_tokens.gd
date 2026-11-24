extends RefCounted

# Phase 7 Unified Design Tokens — macOS Obsidian Translucent Dark System
# Centralized palette, typography, radii, shadows, SVG icon loader & StyleBox helpers.

const SURFACE_PRIMARY := Color(0.0706, 0.0824, 0.1098, 0.92)       # #12151CEB
const SURFACE_ELEVATED := Color(0.1176, 0.1373, 0.1882, 0.85)      # #1E2330D9
const SURFACE_HOVER := Color(0.1686, 0.2000, 0.2706, 0.92)         # #2B3345EB
const SURFACE_PRESSED := Color(0.2078, 0.2471, 0.3294, 0.95)       # #353F54F2
const SURFACE_INPUT := Color(0.0510, 0.0627, 0.0863, 0.88)         # #0D1016E0
const SURFACE_CHIP := Color(1.0, 1.0, 1.0, 0.07)

const BORDER_HAIRLINE := Color(1.0, 1.0, 1.0, 0.10)                # #FFFFFF1A
const BORDER_SPECULAR_TOP := Color(1.0, 1.0, 1.0, 0.18)            # #FFFFFF2E
const BORDER_FOCUS := Color(0.2196, 0.7412, 0.9725, 0.55)          # #38BDF88C
const DIVIDER := Color(1.0, 1.0, 1.0, 0.07)                        # #FFFFFF12

const TEXT_PRIMARY := Color(0.9529, 0.9608, 0.9725, 1.0)           # #F3F5F8
const TEXT_SECONDARY := Color(0.5804, 0.6196, 0.6824, 1.0)         # #949EAE
const TEXT_MUTED := Color(0.3922, 0.4353, 0.5176, 1.0)             # #646F84

const ACCENT_CYAN := Color(0.2196, 0.7412, 0.9725, 1.0)            # #38BDF8
const ACCENT_CYAN_HOVER := Color(0.3373, 0.8000, 0.9490, 1.0)      # #56CCF2
const ACCENT_CYAN_BG := Color(0.2196, 0.7412, 0.9725, 0.15)        # #38BDF826
const ACCENT_CYAN_BORDER := Color(0.2196, 0.7412, 0.9725, 0.40)

const STATUS_EMERALD := Color(0.2039, 0.8275, 0.6000, 1.0)         # #34D399
const STATUS_AMBER := Color(0.9843, 0.7490, 0.1412, 1.0)           # #FBBF24
const STATUS_RED := Color(0.9725, 0.4431, 0.4431, 1.0)             # #F87171
const STATUS_RED_BG := Color(0.9725, 0.4431, 0.4431, 0.16)
const STATUS_RED_HOVER := Color(0.9725, 0.4431, 0.4431, 0.28)

const SWITCH_OFF := Color(0.2196, 0.2471, 0.3137, 1.0)             # #383F50
const SHADOW_COLOR := Color(0.0, 0.0, 0.0, 0.45)                   # #00000073

const KEYCAP_BG := Color(1.0, 1.0, 1.0, 0.08)
const KEYCAP_BORDER := Color(1.0, 1.0, 1.0, 0.14)

const RADIUS_QUICK_ENTRY := 20
const RADIUS_POPOVER := 16
const RADIUS_PIN_WINDOW := 18
const RADIUS_CAPSULE := 14
const RADIUS_PILL := 16
const RADIUS_BUTTON := 10
const RADIUS_KEYCAP := 6

const FONT_SIZE_TITLE := 15
const FONT_SIZE_INPUT := 15
const FONT_SIZE_BODY := 13
const FONT_SIZE_SMALL := 12
const FONT_SIZE_KEYCAP := 11

static var _ui_font: SystemFont
static var _icon_cache: Dictionary = {}


static func get_ui_font() -> Font:
	if _ui_font == null:
		_ui_font = SystemFont.new()
		_ui_font.font_names = PackedStringArray(["PingFangSC-Regular", "Hiragino Sans GB", "Heiti SC", "SF Pro Text", "Helvetica Neue"])
		_ui_font.oversampling = 2.0
		_ui_font.allow_system_fallback = true
	return _ui_font


static func get_icon(icon_name: String, target_size: int = 16, tint: Color = TEXT_SECONDARY, pixel_scale: float = 1.0) -> Texture2D:
	var cache_key := "%s:%d:%s:%.2f" % [icon_name, target_size, tint.to_html(true), pixel_scale]
	if _icon_cache.has(cache_key):
		return _icon_cache[cache_key]
	var res_path := "res://assets/icons/%s.svg" % icon_name
	var abs_path := ProjectSettings.globalize_path(res_path)
	if not FileAccess.file_exists(abs_path) and not ResourceLoader.exists(res_path):
		return null
	var f := FileAccess.open(res_path, FileAccess.READ)
	if f == null:
		f = FileAccess.open(abs_path, FileAccess.READ)
	if f == null:
		return null
	var svg_str := f.get_as_text()
	f.close()
	var hex := "#" + tint.to_html(false)
	svg_str = svg_str.replace("#FFFFFF", hex)
	var svg_scale := (float(target_size) / 18.0) * pixel_scale
	var img := Image.new()
	var err := img.load_svg_from_string(svg_str, svg_scale)
	if err != OK:
		return null
	if tint.a < 0.999:
		for y in range(img.get_height()):
			for x in range(img.get_width()):
				var c := img.get_pixel(x, y)
				if c.a > 0.0:
					c.a *= tint.a
					img.set_pixel(x, y, c)
	var tex := ImageTexture.create_from_image(img)
	_icon_cache[cache_key] = tex
	return tex


static func make_icon_rect(icon_name: String, target_size: int = 16, tint: Color = TEXT_SECONDARY) -> TextureRect:
	var rect := TextureRect.new()
	rect.texture = get_icon(icon_name, target_size, tint, 2.0)
	rect.custom_minimum_size = Vector2(target_size, target_size)
	rect.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	rect.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	return rect


static func make_panel_style(
	radius: int = RADIUS_POPOVER,
	bg: Color = SURFACE_PRIMARY,
	border: Color = BORDER_HAIRLINE,
	shadow_size: int = 26,
	shadow_offset: Vector2 = Vector2(0, 10)
) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = border
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(radius)
	sb.corner_detail = 12
	sb.shadow_color = SHADOW_COLOR
	sb.shadow_size = shadow_size
	sb.shadow_offset = shadow_offset
	sb.anti_aliasing = true
	sb.anti_aliasing_size = 1.0
	return sb


static func make_capsule_style(
	radius: int = RADIUS_CAPSULE,
	bg: Color = SURFACE_ELEVATED,
	border: Color = BORDER_HAIRLINE,
	pad_x: int = 12,
	pad_y: int = 6,
	shadow_size: int = 18
) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = border
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(radius)
	sb.corner_detail = 10
	sb.content_margin_left = pad_x
	sb.content_margin_right = pad_x
	sb.content_margin_top = pad_y
	sb.content_margin_bottom = pad_y
	sb.shadow_color = SHADOW_COLOR
	sb.shadow_size = shadow_size
	sb.shadow_offset = Vector2(0, 6)
	sb.anti_aliasing = true
	sb.anti_aliasing_size = 1.0
	return sb


static func make_button_style(
	bg: Color,
	border: Color = Color.TRANSPARENT,
	radius: int = RADIUS_BUTTON,
	pad_x: int = 12,
	pad_y: int = 6
) -> StyleBoxFlat:
	var sb := StyleBoxFlat.new()
	sb.bg_color = bg
	sb.border_color = border
	sb.set_border_width_all(1 if border.a > 0.01 else 0)
	sb.set_corner_radius_all(radius)
	sb.corner_detail = 8
	sb.content_margin_left = pad_x
	sb.content_margin_right = pad_x
	sb.content_margin_top = pad_y
	sb.content_margin_bottom = pad_y
	sb.anti_aliasing = true
	sb.anti_aliasing_size = 1.0
	return sb


static func apply_button_states(
	btn: Button,
	normal_bg: Color,
	hover_bg: Color = SURFACE_HOVER,
	pressed_bg: Color = SURFACE_PRESSED,
	border: Color = BORDER_HAIRLINE,
	radius: int = RADIUS_BUTTON,
	text_color: Color = TEXT_PRIMARY,
	pad_x: int = 12,
	pad_y: int = 6,
	hover_text_color: Color = TEXT_PRIMARY
) -> void:
	btn.add_theme_stylebox_override("normal", make_button_style(normal_bg, border, radius, pad_x, pad_y))
	btn.add_theme_stylebox_override("hover", make_button_style(hover_bg, border, radius, pad_x, pad_y))
	btn.add_theme_stylebox_override("pressed", make_button_style(pressed_bg, border, radius, pad_x, pad_y))
	btn.add_theme_stylebox_override("focus", make_button_style(hover_bg, BORDER_FOCUS, radius, pad_x, pad_y))
	btn.add_theme_stylebox_override("disabled", make_button_style(Color(normal_bg.r, normal_bg.g, normal_bg.b, normal_bg.a * 0.45), BORDER_HAIRLINE, radius, pad_x, pad_y))
	btn.add_theme_color_override("font_color", text_color)
	btn.add_theme_color_override("font_hover_color", hover_text_color)
	btn.add_theme_color_override("font_pressed_color", hover_text_color)
	btn.add_theme_color_override("font_focus_color", hover_text_color)
	btn.add_theme_color_override("font_disabled_color", TEXT_MUTED)
	btn.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND


static func make_keycap(text_label: String, font_size: int = FONT_SIZE_KEYCAP) -> PanelContainer:
	var panel := PanelContainer.new()
	panel.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var sb := StyleBoxFlat.new()
	sb.bg_color = KEYCAP_BG
	sb.border_color = KEYCAP_BORDER
	sb.set_border_width_all(1)
	sb.set_corner_radius_all(RADIUS_KEYCAP)
	sb.content_margin_left = 7
	sb.content_margin_right = 7
	sb.content_margin_top = 2
	sb.content_margin_bottom = 2
	panel.add_theme_stylebox_override("panel", sb)

	var lbl := Label.new()
	lbl.text = text_label
	lbl.add_theme_font_size_override("font_size", font_size)
	lbl.add_theme_color_override("font_color", TEXT_SECONDARY)
	lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	lbl.mouse_filter = Control.MOUSE_FILTER_IGNORE
	panel.add_child(lbl)
	return panel


static func attach_specular_top(parent: Control, corner_radius: int) -> void:
	var cr := float(corner_radius)
	var hl := BORDER_SPECULAR_TOP
	parent.draw.connect(func() -> void:
		var w := parent.size.x
		if w <= cr * 2.0 + 4.0:
			return
		parent.draw_line(Vector2(cr, 1.0), Vector2(w - cr, 1.0), hl, 1.0, true)
	)


static func make_divider(margin_x: int = 0) -> MarginContainer:
	var wrap := MarginContainer.new()
	wrap.add_theme_constant_override("margin_left", margin_x)
	wrap.add_theme_constant_override("margin_right", margin_x)
	wrap.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var line := ColorRect.new()
	line.color = DIVIDER
	line.custom_minimum_size = Vector2(0, 1)
	line.mouse_filter = Control.MOUSE_FILTER_IGNORE
	wrap.add_child(line)
	return wrap


static func format_shortcut(raw_shortcut: String) -> String:
	var s := raw_shortcut.strip_edges().to_lower()
	if s.is_empty():
		return ""
	var parts := s.split("+")
	var out: PackedStringArray = []
	for p in parts:
		var token := p.strip_edges()
		match token:
			"cmd", "command", "meta", "super":
				out.append("⌘")
			"opt", "option", "alt":
				out.append("⌥")
			"ctrl", "control":
				out.append("⌃")
			"shift":
				out.append("⇧")
			"space":
				out.append("Space")
			"comma", ",":
				out.append(",")
			"esc", "escape":
				out.append("ESC")
			_:
				out.append(token.to_upper() if token.length() == 1 else token.capitalize())
	return " ".join(out)


static func build_obsidian_theme() -> Theme:
	var theme := Theme.new()
	theme.default_font = get_ui_font()
	theme.default_font_size = FONT_SIZE_BODY

	theme.set_color("font_color", "Label", TEXT_PRIMARY)
	theme.set_color("font_color", "LineEdit", TEXT_PRIMARY)
	theme.set_color("font_placeholder_color", "LineEdit", TEXT_MUTED)
	theme.set_color("caret_color", "LineEdit", ACCENT_CYAN)
	theme.set_color("selection_color", "LineEdit", ACCENT_CYAN_BG)
	theme.set_stylebox("normal", "LineEdit", make_button_style(SURFACE_INPUT, BORDER_HAIRLINE, RADIUS_BUTTON, 10, 7))
	theme.set_stylebox("focus", "LineEdit", make_button_style(SURFACE_INPUT, BORDER_FOCUS, RADIUS_BUTTON, 10, 7))
	theme.set_stylebox("read_only", "LineEdit", make_button_style(SURFACE_CHIP, BORDER_HAIRLINE, RADIUS_BUTTON, 10, 7))

	theme.set_color("font_color", "TextEdit", TEXT_PRIMARY)
	theme.set_color("font_placeholder_color", "TextEdit", TEXT_MUTED)
	theme.set_color("caret_color", "TextEdit", ACCENT_CYAN)
	theme.set_stylebox("normal", "TextEdit", make_button_style(SURFACE_INPUT, BORDER_HAIRLINE, RADIUS_BUTTON, 10, 8))
	theme.set_stylebox("focus", "TextEdit", make_button_style(SURFACE_INPUT, BORDER_FOCUS, RADIUS_BUTTON, 10, 8))

	theme.set_stylebox("panel", "PopupMenu", make_panel_style(12, SURFACE_PRIMARY, BORDER_HAIRLINE, 18, Vector2(0, 6)))
	theme.set_stylebox("hover", "PopupMenu", make_button_style(SURFACE_HOVER, Color.TRANSPARENT, 8, 8, 4))
	theme.set_color("font_color", "PopupMenu", TEXT_PRIMARY)
	theme.set_color("font_hover_color", "PopupMenu", TEXT_PRIMARY)
	theme.set_color("font_separator_color", "PopupMenu", DIVIDER)

	theme.set_stylebox("normal", "OptionButton", make_button_style(SURFACE_ELEVATED, BORDER_HAIRLINE, RADIUS_BUTTON, 10, 6))
	theme.set_stylebox("hover", "OptionButton", make_button_style(SURFACE_HOVER, BORDER_HAIRLINE, RADIUS_BUTTON, 10, 6))
	theme.set_stylebox("pressed", "OptionButton", make_button_style(SURFACE_PRESSED, BORDER_FOCUS, RADIUS_BUTTON, 10, 6))
	theme.set_color("font_color", "OptionButton", TEXT_PRIMARY)
	theme.set_color("font_hover_color", "OptionButton", TEXT_PRIMARY)

	var scroll_grab := StyleBoxFlat.new()
	scroll_grab.bg_color = Color(1.0, 1.0, 1.0, 0.18)
	scroll_grab.set_corner_radius_all(4)
	var scroll_grab_hover := StyleBoxFlat.new()
	scroll_grab_hover.bg_color = Color(1.0, 1.0, 1.0, 0.30)
	scroll_grab_hover.set_corner_radius_all(4)
	theme.set_stylebox("grabber", "VScrollBar", scroll_grab)
	theme.set_stylebox("grabber_highlight", "VScrollBar", scroll_grab_hover)
	theme.set_stylebox("grabber_pressed", "VScrollBar", scroll_grab_hover)
	theme.set_stylebox("scroll", "VScrollBar", StyleBoxEmpty.new())

	theme.set_stylebox("panel", "TooltipPanel", make_capsule_style(8, SURFACE_PRIMARY, BORDER_HAIRLINE, 10, 5, 10))
	theme.set_color("font_color", "TooltipLabel", TEXT_PRIMARY)
	theme.set_font_size("font_size", "TooltipLabel", FONT_SIZE_SMALL)
	return theme
