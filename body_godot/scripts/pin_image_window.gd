extends Window

const UITokens := preload("res://theme/ui_tokens.gd")

const SHADOW_MARGIN := 24

var _outer: MarginContainer
var _card: PanelContainer
var _image_rect: TextureRect
var _hover_bar: PanelContainer
var _pin_btn: Button
var _copy_btn: Button
var _reset_btn: Button
var _close_btn: Button

var _dragging: bool = false
var _drag_mouse_start: Vector2i
var _drag_win_start: Vector2i
var _aspect_ratio: float = 1.45
var _natural_size: Vector2i = Vector2i(400, 280)
var _image_path: String = ""
var _pinned_on_top: bool = true
var _hover_visible: bool = false
var _hover_tween: Tween
var _copy_reset_timer: Timer


func _ready() -> void:
	theme = UITokens.build_obsidian_theme()
	always_on_top = _pinned_on_top
	_build_ui()


func _build_ui() -> void:
	_outer = MarginContainer.new()
	_outer.set_anchors_preset(Control.PRESET_FULL_RECT)
	_outer.add_theme_constant_override("margin_left", SHADOW_MARGIN)
	_outer.add_theme_constant_override("margin_right", SHADOW_MARGIN)
	_outer.add_theme_constant_override("margin_top", SHADOW_MARGIN - 4)
	_outer.add_theme_constant_override("margin_bottom", SHADOW_MARGIN + 6)
	add_child(_outer)

	_card = PanelContainer.new()
	var card_sb := UITokens.make_panel_style(
		UITokens.RADIUS_PIN_WINDOW,
		UITokens.SURFACE_PRIMARY,
		UITokens.BORDER_HAIRLINE,
		24,
		Vector2(0, 10)
	)
	card_sb.set_content_margin_all(0)
	_card.add_theme_stylebox_override("panel", card_sb)
	_outer.add_child(_card)

	var content := Control.new()
	content.clip_contents = true
	content.mouse_filter = Control.MOUSE_FILTER_STOP
	content.gui_input.connect(_on_content_gui_input)
	_card.add_child(content)

	_image_rect = TextureRect.new()
	_image_rect.set_anchors_preset(Control.PRESET_FULL_RECT)
	_image_rect.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	_image_rect.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	_image_rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_image_rect.material = _make_rounded_clip_material(float(UITokens.RADIUS_PIN_WINDOW))
	_image_rect.resized.connect(_update_shader_rect_size)
	content.add_child(_image_rect)

	UITokens.attach_specular_top(content, UITokens.RADIUS_PIN_WINDOW)

	# Floating top-right hover control capsule (0.15s fade in/out)
	_hover_bar = PanelContainer.new()
	_hover_bar.modulate.a = 0.0
	_hover_bar.mouse_filter = Control.MOUSE_FILTER_STOP
	_hover_bar.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(
			UITokens.RADIUS_PILL,
			UITokens.SURFACE_PRIMARY,
			UITokens.BORDER_HAIRLINE,
			8,
			4,
			14
		)
	)
	_hover_bar.set_anchors_preset(Control.PRESET_TOP_RIGHT)
	_hover_bar.anchor_left = 1.0
	_hover_bar.anchor_right = 1.0
	_hover_bar.anchor_top = 0.0
	_hover_bar.anchor_bottom = 0.0
	_hover_bar.grow_horizontal = Control.GROW_DIRECTION_BEGIN
	_hover_bar.offset_right = -12
	_hover_bar.offset_top = 12
	content.add_child(_hover_bar)

	var bar_hbox := HBoxContainer.new()
	bar_hbox.add_theme_constant_override("separation", 4)
	_hover_bar.add_child(bar_hbox)

	_pin_btn = _make_pill_icon_button("pin", "取消/恢复始终置顶", true)
	_pin_btn.pressed.connect(_toggle_pin_top)
	bar_hbox.add_child(_pin_btn)

	_copy_btn = _make_pill_icon_button("copy", "复制图片到剪贴板", false)
	_copy_btn.pressed.connect(_copy_image_to_clipboard)
	bar_hbox.add_child(_copy_btn)

	_reset_btn = Button.new()
	_reset_btn.text = "100%"
	_reset_btn.tooltip_text = "恢复原始尺寸"
	_reset_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	UITokens.apply_button_states(
		_reset_btn,
		Color.TRANSPARENT,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		Color.TRANSPARENT,
		UITokens.RADIUS_PILL,
		UITokens.TEXT_SECONDARY,
		8,
		4,
		UITokens.TEXT_PRIMARY
	)
	_reset_btn.pressed.connect(reset_to_original_size)
	bar_hbox.add_child(_reset_btn)

	_close_btn = _make_pill_icon_button("close", "关闭悬浮窗 (Esc)", false)
	_close_btn.pressed.connect(queue_free)
	bar_hbox.add_child(_close_btn)

	_copy_reset_timer = Timer.new()
	_copy_reset_timer.one_shot = true
	_copy_reset_timer.wait_time = 1.1
	_copy_reset_timer.timeout.connect(func() -> void:
		if _copy_btn != null:
			_copy_btn.icon = UITokens.get_icon("copy", 14, UITokens.TEXT_SECONDARY, 2.0)
	)
	add_child(_copy_reset_timer)


func _make_pill_icon_button(icon_name: String, tip: String, highlighted: bool) -> Button:
	var btn := Button.new()
	btn.custom_minimum_size = Vector2(26, 26)
	btn.tooltip_text = tip
	btn.expand_icon = true
	btn.icon_alignment = HORIZONTAL_ALIGNMENT_CENTER
	btn.add_theme_constant_override("icon_max_width", 14)
	_style_pill_icon_button(btn, icon_name, highlighted)
	return btn


func _style_pill_icon_button(btn: Button, icon_name: String, highlighted: bool) -> void:
	var tint := UITokens.ACCENT_CYAN if highlighted else UITokens.TEXT_SECONDARY
	btn.icon = UITokens.get_icon(icon_name, 14, tint, 2.0)
	UITokens.apply_button_states(
		btn,
		UITokens.ACCENT_CYAN_BG if highlighted else Color.TRANSPARENT,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		Color.TRANSPARENT,
		13,
		tint,
		4,
		4,
		UITokens.TEXT_PRIMARY
	)


func _make_rounded_clip_material(radius_px: float) -> ShaderMaterial:
	var shader := Shader.new()
	shader.code = """
shader_type canvas_item;
uniform vec2 rect_size = vec2(360.0, 240.0);
uniform float corner_radius = 18.0;

void fragment() {
	vec4 tex = texture(TEXTURE, UV);
	vec2 pos = UV * rect_size;
	vec2 half_size = rect_size * 0.5;
	vec2 d = abs(pos - half_size) - (half_size - vec2(corner_radius));
	float dist = length(max(d, vec2(0.0))) + min(max(d.x, d.y), 0.0) - corner_radius;
	float alpha = 1.0 - smoothstep(-0.75, 0.75, dist);
	COLOR = vec4(tex.rgb, tex.a * alpha);
}
"""
	var mat := ShaderMaterial.new()
	mat.shader = shader
	mat.set_shader_parameter("corner_radius", radius_px)
	return mat


func _update_shader_rect_size() -> void:
	if _image_rect != null and _image_rect.material is ShaderMaterial:
		var mat := _image_rect.material as ShaderMaterial
		mat.set_shader_parameter("rect_size", _image_rect.size)


func _process(_delta: float) -> void:
	if not visible:
		return
	var mouse_pos := DisplayServer.mouse_get_position()
	var card_global := Rect2(position + Vector2i(SHADOW_MARGIN, SHADOW_MARGIN - 4), Vector2(size.x - SHADOW_MARGIN * 2, size.y - SHADOW_MARGIN * 2))
	var inside := card_global.has_point(Vector2(mouse_pos)) or _dragging
	if inside != _hover_visible:
		_set_hover_visible(inside)


func _set_hover_visible(show_bar: bool) -> void:
	_hover_visible = show_bar
	if _hover_bar == null:
		return
	if _hover_tween != null and _hover_tween.is_running():
		_hover_tween.kill()
	_hover_tween = create_tween()
	_hover_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_hover_tween.tween_property(_hover_bar, "modulate:a", 1.0 if show_bar else 0.0, 0.15)


func load_image_from_path(path: String) -> bool:
	return load_image(path)


func load_image(path: String) -> bool:
	var img := Image.load_from_file(path)
	if img == null or img.is_empty():
		return false
	_image_path = path
	var tex := ImageTexture.create_from_image(img)
	_image_rect.texture = tex

	var w := float(img.get_width())
	var h := float(img.get_height())
	_aspect_ratio = max(0.25, w / max(1.0, h))
	_natural_size = Vector2i(int(w), int(h))

	var target_w: int = clampi(int(w), 240, 620)
	var target_h: int = clampi(int(float(target_w) / _aspect_ratio), 160, 520)
	if target_h >= 520:
		target_w = int(float(target_h) * _aspect_ratio)
	size = Vector2i(target_w + SHADOW_MARGIN * 2, target_h + SHADOW_MARGIN * 2 + 2)
	title = "Qimingxing Pin — %s" % path.get_file()
	call_deferred("_update_shader_rect_size")
	return true


func reset_to_original_size() -> void:
	var usable := DisplayServer.screen_get_usable_rect()
	var max_w := maxi(240, usable.size.x - 120)
	var max_h := maxi(180, usable.size.y - 120)
	var target_w := _natural_size.x
	var target_h := _natural_size.y
	if target_w > max_w or target_h > max_h:
		var scale := minf(float(max_w) / maxf(1.0, float(target_w)), float(max_h) / maxf(1.0, float(target_h)))
		target_w = int(float(target_w) * scale)
		target_h = int(float(target_h) * scale)
	var new_size := Vector2i(target_w + SHADOW_MARGIN * 2, target_h + SHADOW_MARGIN * 2 + 2)
	var center := position + size / 2
	size = new_size
	position = center - new_size / 2
	call_deferred("_update_shader_rect_size")


func _toggle_pin_top() -> void:
	_pinned_on_top = not _pinned_on_top
	always_on_top = _pinned_on_top
	if _pin_btn != null:
		_style_pill_icon_button(_pin_btn, "pin", _pinned_on_top)


func _copy_image_to_clipboard() -> void:
	if _image_path.is_empty():
		return
	var safe_path := _image_path.replace("\\", "\\\\").replace("\"", "\\\"")
	var ext := _image_path.get_extension().to_lower()
	var script_cmd := ""
	if ext == "png":
		script_cmd = "set the clipboard to (read (POSIX file \"%s\") as «class PNGf»)" % safe_path
		OS.execute("osascript", ["-e", script_cmd])
	else:
		var tmp_png := "/tmp/qimingxing_pin_copy.png"
		var sips_rc := OS.execute("sips", ["-s", "format", "png", _image_path, "--out", tmp_png])
		if sips_rc == 0:
			script_cmd = "set the clipboard to (read (POSIX file \"%s\") as «class PNGf»)" % tmp_png
			OS.execute("osascript", ["-e", script_cmd])
		else:
			DisplayServer.clipboard_set(_image_path)
	if _copy_btn != null:
		_copy_btn.icon = UITokens.get_icon("check", 14, UITokens.STATUS_EMERALD, 2.0)
	if _copy_reset_timer != null:
		_copy_reset_timer.start()


func _on_content_gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.double_click:
				queue_free()
				return
			if mb.pressed:
				_dragging = true
				_drag_mouse_start = DisplayServer.mouse_get_position()
				_drag_win_start = position
			else:
				_dragging = false
		elif mb.pressed and (mb.button_index == MOUSE_BUTTON_WHEEL_UP or mb.button_index == MOUSE_BUTTON_WHEEL_DOWN):
			var delta_w := 36 if mb.button_index == MOUSE_BUTTON_WHEEL_UP else -36
			var img_w := size.x - SHADOW_MARGIN * 2
			var new_img_w: int = clampi(img_w + delta_w, 200, 1040)
			var new_img_h: int = clampi(int(float(new_img_w) / _aspect_ratio), 120, 860)
			size = Vector2i(new_img_w + SHADOW_MARGIN * 2, new_img_h + SHADOW_MARGIN * 2 + 2)
			call_deferred("_update_shader_rect_size")
	elif event is InputEventMouseMotion and _dragging:
		var cur := DisplayServer.mouse_get_position()
		position = _drag_win_start + (cur - _drag_mouse_start)


func _unhandled_key_input(event: InputEvent) -> void:
	if event is InputEventKey:
		var kev := event as InputEventKey
		if kev.pressed and kev.keycode == KEY_ESCAPE:
			queue_free()
