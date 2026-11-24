extends Window

signal action_triggered(action_id: int)
signal toggle_changed(action_id: int, enabled: bool)

const UITokens := preload("res://theme/ui_tokens.gd")
const ToggleSwitch := preload("res://theme/toggle_switch.gd")

const ID_SETTINGS := 1
const ID_ROAM := 2
const ID_EARCONS := 3
const ID_AMBIENT := 4
const ID_RESET_POS := 5
const ID_RELOAD_MCP := 6
const ID_OPEN_WEB := 7
const ID_CHAT_INPUT := 8
const ID_SHORTCUTS := 9
const ID_MCP_CONFIG := 10
const ID_QUIT := 99

const MARGIN_X := 20
const MARGIN_TOP := 12
const MARGIN_BOTTOM := 24

var _outer: MarginContainer
var _card: PanelContainer
var _status_dot: StatusDot
var _status_label: Label
var _chat_keycap_lbl: Label
var _settings_keycap_lbl: Label
var _roam_sw: Control
var _earcons_sw: Control
var _ambient_sw: Control
var _anim_tween: Tween
var _suppress_focus_close_until_ms: int = 0


func _ready() -> void:
	theme = UITokens.build_obsidian_theme()
	_build_ui()
	focus_exited.connect(_on_focus_exited)
	close_requested.connect(hide_popover)


func _build_ui() -> void:
	_outer = MarginContainer.new()
	_outer.set_anchors_preset(Control.PRESET_FULL_RECT)
	_outer.add_theme_constant_override("margin_left", MARGIN_X)
	_outer.add_theme_constant_override("margin_right", MARGIN_X)
	_outer.add_theme_constant_override("margin_top", MARGIN_TOP)
	_outer.add_theme_constant_override("margin_bottom", MARGIN_BOTTOM)
	add_child(_outer)

	_card = PanelContainer.new()
	var sb := UITokens.make_panel_style(
		UITokens.RADIUS_POPOVER,
		UITokens.SURFACE_PRIMARY,
		UITokens.BORDER_HAIRLINE,
		24,
		Vector2(0, 10)
	)
	sb.content_margin_left = 10
	sb.content_margin_right = 10
	sb.content_margin_top = 12
	sb.content_margin_bottom = 10
	_card.add_theme_stylebox_override("panel", sb)
	_outer.add_child(_card)
	UITokens.attach_specular_top(_card, UITokens.RADIUS_POPOVER)

	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 4)
	_card.add_child(col)

	# 1. Header: "启明星" + ● 运行中 · Profile
	var header := MarginContainer.new()
	header.add_theme_constant_override("margin_left", 8)
	header.add_theme_constant_override("margin_right", 8)
	header.add_theme_constant_override("margin_top", 2)
	header.add_theme_constant_override("margin_bottom", 6)
	col.add_child(header)

	var hrow := HBoxContainer.new()
	hrow.add_theme_constant_override("separation", 8)
	header.add_child(hrow)

	var brand := Label.new()
	brand.text = "启明星"
	brand.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	brand.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_TITLE)
	brand.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	hrow.add_child(brand)

	_status_dot = StatusDot.new()
	_status_dot.dot_color = UITokens.STATUS_EMERALD
	hrow.add_child(_status_dot)

	_status_label = Label.new()
	_status_label.text = "运行中"
	_status_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	_status_label.add_theme_color_override("font_color", UITokens.TEXT_SECONDARY)
	hrow.add_child(_status_label)

	col.add_child(UITokens.make_divider(4))

	# 2. Primary actions + keycaps
	var chat_res := _add_action_row(col, ID_CHAT_INPUT, "chat", "呼出输入框", "⌥ Space")
	_chat_keycap_lbl = chat_res["keycap_label"]
	var set_res := _add_action_row(col, ID_SETTINGS, "settings", "设置与 API Key...", "⌘ ,")
	_settings_keycap_lbl = set_res["keycap_label"]
	_add_action_row(col, ID_SHORTCUTS, "keyboard", "快捷键管理...", "")
	_add_action_row(col, ID_MCP_CONFIG, "plug", "MCP 服务配置...", "")

	col.add_child(UITokens.make_divider(4))

	# 3. Animated ToggleSwitches
	_roam_sw = _add_toggle_row(col, ID_ROAM, "roaming", "屏幕自由漫步", true)
	_earcons_sw = _add_toggle_row(col, ID_EARCONS, "audio_wave", "状态微音效", true)
	_ambient_sw = _add_toggle_row(col, ID_AMBIENT, "music", "背景氛围音", false)

	col.add_child(UITokens.make_divider(4))

	# 4. Utility actions
	_add_action_row(col, ID_RESET_POS, "reset", "重置位置与取消贴边", "")
	_add_action_row(col, ID_RELOAD_MCP, "refresh", "重载 MCP 工具", "")
	_add_action_row(col, ID_OPEN_WEB, "external_link", "打开 Web 调试台", "")

	col.add_child(UITokens.make_divider(4))

	# 5. Quit row
	_add_action_row(col, ID_QUIT, "power", "退出启明星", "⌘ Q", true)

	call_deferred("_sync_height")


func _add_action_row(
	parent: Control,
	action_id: int,
	icon_name: String,
	label_text: String,
	keycap_text: String,
	is_danger: bool = false
) -> Dictionary:
	var row_btn := Button.new()
	row_btn.custom_minimum_size = Vector2(0, 34)
	UITokens.apply_button_states(
		row_btn,
		Color.TRANSPARENT,
		UITokens.STATUS_RED_BG if is_danger else UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		Color.TRANSPARENT,
		UITokens.RADIUS_BUTTON,
		UITokens.TEXT_PRIMARY,
		8,
		4
	)
	parent.add_child(row_btn)

	var inner := HBoxContainer.new()
	inner.set_anchors_preset(Control.PRESET_FULL_RECT)
	inner.offset_left = 10
	inner.offset_right = -10
	inner.add_theme_constant_override("separation", 10)
	inner.mouse_filter = Control.MOUSE_FILTER_IGNORE
	row_btn.add_child(inner)

	var icon_tint := UITokens.STATUS_RED if is_danger else UITokens.TEXT_SECONDARY
	var icon_rect := UITokens.make_icon_rect(icon_name, 16, icon_tint)
	inner.add_child(icon_rect)

	var lbl := Label.new()
	lbl.text = label_text
	lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
	lbl.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	lbl.mouse_filter = Control.MOUSE_FILTER_IGNORE
	inner.add_child(lbl)

	var cap_lbl: Label = null
	if keycap_text != "":
		var cap := UITokens.make_keycap(keycap_text, UITokens.FONT_SIZE_KEYCAP)
		cap.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		inner.add_child(cap)
		cap_lbl = cap.get_child(0) as Label

	row_btn.pressed.connect(func() -> void:
		hide_popover()
		action_triggered.emit(action_id)
	)
	return {"button": row_btn, "keycap_label": cap_lbl}


func _add_toggle_row(
	parent: Control,
	action_id: int,
	icon_name: String,
	label_text: String,
	initial: bool
) -> Control:
	var row_wrap := MarginContainer.new()
	row_wrap.custom_minimum_size = Vector2(0, 34)
	row_wrap.add_theme_constant_override("margin_left", 10)
	row_wrap.add_theme_constant_override("margin_right", 10)
	parent.add_child(row_wrap)

	var hbox := HBoxContainer.new()
	hbox.add_theme_constant_override("separation", 10)
	row_wrap.add_child(hbox)

	var icon_rect := UITokens.make_icon_rect(icon_name, 16, UITokens.TEXT_SECONDARY)
	hbox.add_child(icon_rect)

	var lbl := Label.new()
	lbl.text = label_text
	lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
	lbl.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	hbox.add_child(lbl)

	var sw: Control = ToggleSwitch.new()
	sw.checked = initial
	sw.toggled.connect(func(is_on: bool) -> void:
		toggle_changed.emit(action_id, is_on)
	)
	hbox.add_child(sw)
	return sw


func _sync_height() -> void:
	if _card == null:
		return
	var target_h := int(ceil(_card.get_combined_minimum_size().y)) + MARGIN_TOP + MARGIN_BOTTOM
	size = Vector2i(size.x, maxi(420, target_h))


func open_near_rect(anchor_rect: Rect2) -> void:
	_sync_height()
	var usable := DisplayServer.screen_get_usable_rect()
	var px := int(anchor_rect.position.x + anchor_rect.size.x * 0.5 - float(size.x) * 0.5)
	var py := int(anchor_rect.position.y + anchor_rect.size.y) + 4
	if anchor_rect.size.x <= 1.0 and anchor_rect.size.y <= 1.0:
		var m := DisplayServer.mouse_get_position()
		px = m.x - int(size.x * 0.5)
		py = usable.position.y + 6
	px = clampi(px, usable.position.x + 8, usable.position.x + usable.size.x - size.x - 8)
	py = clampi(py, usable.position.y + 2, usable.position.y + usable.size.y - size.y - 8)
	position = Vector2i(px, py)

	_suppress_focus_close_until_ms = Time.get_ticks_msec() + 260
	if _anim_tween != null and _anim_tween.is_running():
		_anim_tween.kill()

	visible = true
	grab_focus()
	if _outer != null and _card != null:
		_outer.modulate.a = 0.0
		_card.pivot_offset = Vector2(_card.size.x * 0.5, 0.0)
		_card.scale = Vector2(0.96, 0.96)
		_anim_tween = create_tween().set_parallel(true)
		_anim_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
		_anim_tween.tween_property(_outer, "modulate:a", 1.0, 0.14)
		_anim_tween.tween_property(_card, "scale", Vector2.ONE, 0.14)


func hide_popover() -> void:
	if not visible:
		return
	if _anim_tween != null and _anim_tween.is_running():
		_anim_tween.kill()
	visible = false


func _on_focus_exited() -> void:
	if Time.get_ticks_msec() < _suppress_focus_close_until_ms:
		return
	hide_popover()


func _unhandled_key_input(event: InputEvent) -> void:
	if visible and event is InputEventKey:
		var kev := event as InputEventKey
		if kev.pressed and kev.keycode == KEY_ESCAPE:
			hide_popover()


func sync_toggles(roam_on: bool, earcons_on: bool, ambient_on: bool) -> void:
	if _roam_sw != null:
		_roam_sw.call("set_checked", roam_on, false)
	if _earcons_sw != null:
		_earcons_sw.call("set_checked", earcons_on, false)
	if _ambient_sw != null:
		_ambient_sw.call("set_checked", ambient_on, false)


func sync_status(connected: bool, active_profile: String) -> void:
	if _status_dot != null:
		_status_dot.dot_color = UITokens.STATUS_EMERALD if connected else UITokens.STATUS_AMBER
		_status_dot.queue_redraw()
	if _status_label != null:
		var state_txt := "运行中" if connected else "重连中"
		if active_profile.strip_edges() != "":
			_status_label.text = "%s · %s" % [state_txt, active_profile]
		else:
			_status_label.text = state_txt


func sync_shortcut_hints(chat_hk: String, settings_hk: String = "") -> void:
	if _chat_keycap_lbl != null:
		var fmt := UITokens.format_shortcut(chat_hk)
		_chat_keycap_lbl.text = fmt if fmt != "" else "未设"
	if _settings_keycap_lbl != null and settings_hk.strip_edges() != "":
		var fmt_s := UITokens.format_shortcut(settings_hk)
		_settings_keycap_lbl.text = fmt_s if fmt_s != "" else "⌘ ,"


class StatusDot extends Control:
	var dot_color: Color = Color(0.2039, 0.8275, 0.6000, 1.0)

	func _init() -> void:
		custom_minimum_size = Vector2(8, 16)
		size_flags_vertical = Control.SIZE_SHRINK_CENTER
		mouse_filter = Control.MOUSE_FILTER_IGNORE

	func _draw() -> void:
		draw_circle(Vector2(4.0, size.y * 0.5), 3.3, dot_color)
