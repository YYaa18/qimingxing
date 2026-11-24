extends Control
class_name QimingxingUIOverlay

signal passthrough_rects_changed(rects: Array[Rect2])
signal approval_decided(approval_id: String, turn_id: String, approved: bool)
signal clarification_answered(approval_id: String, turn_id: String, approved: bool, answer_text: String)
signal cancel_requested(turn_id: String)
signal drop_queue_submitted(paths: Array[String])
signal desktop_action_requested(action: String, path: String, media_type: String)

const UITokens := preload("res://theme/ui_tokens.gd")
const PIN_WINDOW_SCENE := preload("res://scenes/PinImageWindow.tscn")
const VIEWPORT_WIDTH := 420.0
const AVATAR_CENTER_X := 210.0
const AVATAR_NECK_Y := 286.0
const BUBBLE_MIN_W := 148.0
const BUBBLE_MAX_W := 296.0
const STACK_WIDTH := 348.0

var _bubble_panel: PanelContainer
var _bubble_label: RichTextLabel
var _bubble_fade_timer: float = 0.0
var _bubble_target_w: float = BUBBLE_MAX_W
var _current_spoken: String = ""
var _current_turn_id: String = ""
var _in_turn: bool = false
var _avatar_scale: float = 0.78

var _stack: VBoxContainer
var _approval_panel: PanelContainer
var _approval_label: Label
var _pending_approval_id: String = ""
var _pending_approval_turn: String = ""

var _clarify_panel: PanelContainer
var _clarify_label: Label
var _clarify_options_box: HBoxContainer
var _clarify_input: LineEdit
var _pending_clarify_id: String = ""
var _pending_clarify_turn: String = ""

var _drop_panel: PanelContainer
var _drop_label: Label
var _dropped_paths: Array[String] = []

var _artifact_panel: PanelContainer
var _artifact_title_btn: Button
var _artifact_buttons_box: HBoxContainer
var _artifact_path: String = ""
var _artifact_media_type: String = "file"

var _controls_bar: HBoxContainer
var _stop_btn: Button

signal interaction_active_changed(active: bool)

var _dock_tucked: bool = false


func _ready() -> void:
	theme = UITokens.build_obsidian_theme()
	_build_ui()
	_emit_rects()


func _build_ui() -> void:
	# 1. Transient 1-3 sentence speech bubble anchored above avatar head
	_bubble_panel = PanelContainer.new()
	_bubble_panel.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(
			UITokens.RADIUS_CAPSULE,
			UITokens.SURFACE_PRIMARY,
			UITokens.BORDER_HAIRLINE,
			12,
			8,
			18
		)
	)
	_bubble_panel.position = Vector2((VIEWPORT_WIDTH - BUBBLE_MAX_W) * 0.5, 130.0)
	_bubble_panel.custom_minimum_size = Vector2(BUBBLE_MAX_W, 0.0)
	_bubble_panel.visible = false
	add_child(_bubble_panel)

	_bubble_label = RichTextLabel.new()
	_bubble_label.fit_content = true
	_bubble_label.scroll_active = false
	_bubble_label.bbcode_enabled = false
	_bubble_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_bubble_label.add_theme_font_size_override("normal_font_size", UITokens.FONT_SIZE_BODY)
	_bubble_label.add_theme_color_override("default_color", UITokens.TEXT_PRIMARY)
	_bubble_panel.add_child(_bubble_label)

	# Bottom capsule stack anchored cleanly under scaled avatar halo
	_stack = VBoxContainer.new()
	_stack.custom_minimum_size = Vector2(STACK_WIDTH, 0.0)
	_stack.size = Vector2(STACK_WIDTH, 0.0)
	_stack.add_theme_constant_override("separation", 8)
	_stack.mouse_filter = Control.MOUSE_FILTER_PASS
	add_child(_stack)
	_reposition_stack()

	# 2. Approval / Tool confirmation capsule
	_approval_panel = PanelContainer.new()
	_approval_panel.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(
			UITokens.RADIUS_CAPSULE,
			UITokens.SURFACE_PRIMARY,
			Color(0.9843, 0.7490, 0.1412, 0.45),
			12,
			8,
			16
		)
	)
	_approval_panel.visible = false
	_stack.add_child(_approval_panel)

	var app_row := HBoxContainer.new()
	app_row.add_theme_constant_override("separation", 8)
	_approval_panel.add_child(app_row)

	var app_icon := UITokens.make_icon_rect("stop", 15, UITokens.STATUS_AMBER)
	app_row.add_child(app_icon)

	_approval_label = Label.new()
	_approval_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_approval_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_approval_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	_approval_label.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	app_row.add_child(_approval_label)

	var allow_btn := Button.new()
	allow_btn.text = "允许"
	allow_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	UITokens.apply_button_states(
		allow_btn,
		UITokens.ACCENT_CYAN,
		UITokens.ACCENT_CYAN_HOVER,
		UITokens.ACCENT_CYAN,
		Color.TRANSPARENT,
		UITokens.RADIUS_BUTTON,
		Color(0.05, 0.08, 0.12),
		10,
		4,
		Color(0.05, 0.08, 0.12)
	)
	allow_btn.pressed.connect(func() -> void: _resolve_approval(true))
	app_row.add_child(allow_btn)

	var deny_btn := Button.new()
	deny_btn.text = "拒绝"
	deny_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	UITokens.apply_button_states(
		deny_btn,
		UITokens.STATUS_RED_BG,
		UITokens.STATUS_RED.darkened(0.45),
		UITokens.STATUS_RED_BG,
		Color(0.9725, 0.4431, 0.4431, 0.35),
		UITokens.RADIUS_BUTTON,
		UITokens.STATUS_RED,
		10,
		4,
		UITokens.TEXT_PRIMARY
	)
	deny_btn.pressed.connect(func() -> void: _resolve_approval(false))
	app_row.add_child(deny_btn)

	# 2.5 Clarification prompt capsule (ask_user, unified Obsidian surface)
	_clarify_panel = PanelContainer.new()
	_clarify_panel.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(
			UITokens.RADIUS_CAPSULE,
			UITokens.SURFACE_PRIMARY,
			UITokens.ACCENT_CYAN_BORDER,
			12,
			10,
			18
		)
	)
	_clarify_panel.visible = false
	_stack.add_child(_clarify_panel)

	var clar_col := VBoxContainer.new()
	clar_col.add_theme_constant_override("separation", 7)
	_clarify_panel.add_child(clar_col)

	var clar_hdr := HBoxContainer.new()
	clar_hdr.add_theme_constant_override("separation", 7)
	clar_col.add_child(clar_hdr)
	clar_hdr.add_child(UITokens.make_icon_rect("chat", 15, UITokens.ACCENT_CYAN))

	_clarify_label = Label.new()
	_clarify_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_clarify_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_clarify_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	_clarify_label.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	clar_hdr.add_child(_clarify_label)

	_clarify_options_box = HBoxContainer.new()
	_clarify_options_box.add_theme_constant_override("separation", 6)
	clar_col.add_child(_clarify_options_box)

	var clar_input_row := HBoxContainer.new()
	clar_input_row.add_theme_constant_override("separation", 6)
	clar_col.add_child(clar_input_row)

	_clarify_input = LineEdit.new()
	_clarify_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_clarify_input.placeholder_text = "输入你的回复…"
	_clarify_input.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	_clarify_input.text_submitted.connect(func(txt: String) -> void:
		var cleaned := txt.strip_edges()
		if cleaned != "":
			_resolve_clarification(true, cleaned)
	)
	clar_input_row.add_child(_clarify_input)

	var clar_ok := Button.new()
	clar_ok.text = "回复"
	clar_ok.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	UITokens.apply_button_states(
		clar_ok,
		UITokens.ACCENT_CYAN,
		UITokens.ACCENT_CYAN_HOVER,
		UITokens.ACCENT_CYAN,
		Color.TRANSPARENT,
		UITokens.RADIUS_BUTTON,
		Color(0.05, 0.08, 0.12),
		10,
		4,
		Color(0.05, 0.08, 0.12)
	)
	clar_ok.pressed.connect(func() -> void:
		var cleaned := _clarify_input.text.strip_edges()
		if cleaned != "":
			_resolve_clarification(true, cleaned)
	)
	clar_input_row.add_child(clar_ok)

	var clar_cancel := Button.new()
	clar_cancel.text = "取消"
	clar_cancel.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	UITokens.apply_button_states(
		clar_cancel,
		UITokens.SURFACE_CHIP,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		UITokens.BORDER_HAIRLINE,
		UITokens.RADIUS_BUTTON,
		UITokens.TEXT_SECONDARY,
		9,
		4
	)
	clar_cancel.pressed.connect(func() -> void: _resolve_clarification(false, ""))
	clar_input_row.add_child(clar_cancel)

	# 3. Drop Queue Capsule: [folder icon] 已选 N 项文件 [投喂挂载] [×]
	_drop_panel = PanelContainer.new()
	_drop_panel.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(
			24,
			UITokens.SURFACE_PRIMARY,
			UITokens.BORDER_HAIRLINE,
			12,
			8,
			20
		)
	)
	_drop_panel.visible = false
	_stack.add_child(_drop_panel)

	var drop_row := HBoxContainer.new()
	drop_row.add_theme_constant_override("separation", 10)
	_drop_panel.add_child(drop_row)

	drop_row.add_child(UITokens.make_icon_rect("folder", 18, UITokens.ACCENT_CYAN))

	_drop_label = Label.new()
	_drop_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_drop_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
	_drop_label.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	drop_row.add_child(_drop_label)

	var send_drop := Button.new()
	send_drop.text = "投喂挂载"
	send_drop.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	UITokens.apply_button_states(
		send_drop,
		UITokens.ACCENT_CYAN,
		UITokens.ACCENT_CYAN_HOVER,
		UITokens.ACCENT_CYAN,
		Color.TRANSPARENT,
		UITokens.RADIUS_PILL,
		Color(0.05, 0.08, 0.12),
		14,
		6,
		Color(0.05, 0.08, 0.12)
	)
	send_drop.pressed.connect(_submit_drop_queue)
	drop_row.add_child(send_drop)

	var clr_drop := Button.new()
	clr_drop.icon = UITokens.get_icon("close", 12, UITokens.TEXT_SECONDARY, 1.0)
	clr_drop.expand_icon = false
	clr_drop.icon_alignment = HORIZONTAL_ALIGNMENT_CENTER
	clr_drop.custom_minimum_size = Vector2(26, 26)
	clr_drop.tooltip_text = "清空待挂载队列"
	UITokens.apply_button_states(
		clr_drop,
		UITokens.SURFACE_CHIP,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		UITokens.BORDER_HAIRLINE,
		11,
		UITokens.TEXT_SECONDARY,
		4,
		4
	)
	clr_drop.pressed.connect(clear_drop_queue)
	drop_row.add_child(clr_drop)

	# 4. Artifact Delivery Card: [file icon] [clickable filename] [×] + [预览(primary) | 复制 | 访达 | 钉住]
	_artifact_panel = PanelContainer.new()
	_artifact_panel.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(
			UITokens.RADIUS_CAPSULE,
			UITokens.SURFACE_PRIMARY,
			UITokens.BORDER_HAIRLINE,
			12,
			10,
			20
		)
	)
	_artifact_panel.visible = false
	_stack.add_child(_artifact_panel)
	UITokens.attach_specular_top(_artifact_panel, UITokens.RADIUS_CAPSULE)

	var art_col := VBoxContainer.new()
	art_col.add_theme_constant_override("separation", 8)
	_artifact_panel.add_child(art_col)

	var art_top := HBoxContainer.new()
	art_top.add_theme_constant_override("separation", 8)
	art_col.add_child(art_top)

	art_top.add_child(UITokens.make_icon_rect("file", 18, UITokens.TEXT_PRIMARY))

	_artifact_title_btn = Button.new()
	_artifact_title_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_artifact_title_btn.alignment = HORIZONTAL_ALIGNMENT_LEFT
	_artifact_title_btn.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_artifact_title_btn.tooltip_text = "点击直接打开文件"
	_artifact_title_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
	UITokens.apply_button_states(
		_artifact_title_btn,
		Color.TRANSPARENT,
		UITokens.SURFACE_CHIP,
		UITokens.SURFACE_HOVER,
		Color.TRANSPARENT,
		6,
		UITokens.TEXT_PRIMARY,
		4,
		2,
		UITokens.ACCENT_CYAN_HOVER
	)
	_artifact_title_btn.pressed.connect(func() -> void:
		if _artifact_path != "":
			desktop_action_requested.emit("open_external", _artifact_path, _artifact_media_type)
	)
	art_top.add_child(_artifact_title_btn)

	var close_art := Button.new()
	close_art.icon = UITokens.get_icon("close", 12, UITokens.TEXT_SECONDARY, 1.0)
	close_art.expand_icon = false
	close_art.icon_alignment = HORIZONTAL_ALIGNMENT_CENTER
	close_art.custom_minimum_size = Vector2(22, 22)
	close_art.tooltip_text = "收起产物卡片"
	UITokens.apply_button_states(
		close_art,
		Color.TRANSPARENT,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		Color.TRANSPARENT,
		9,
		UITokens.TEXT_SECONDARY,
		4,
		4
	)
	close_art.pressed.connect(func() -> void:
		_artifact_panel.visible = false
		_emit_rects()
	)
	art_top.add_child(close_art)

	_artifact_buttons_box = HBoxContainer.new()
	_artifact_buttons_box.add_theme_constant_override("separation", 6)
	art_col.add_child(_artifact_buttons_box)

	# 5. Minimal emergency-stop capsule (hidden cleanly whenever idle)
	_controls_bar = HBoxContainer.new()
	_controls_bar.alignment = BoxContainer.ALIGNMENT_CENTER
	_controls_bar.add_theme_constant_override("separation", 6)
	_controls_bar.visible = false
	_stack.add_child(_controls_bar)

	_stop_btn = Button.new()
	_stop_btn.text = "急停"
	_stop_btn.icon = UITokens.get_icon("stop", 12, UITokens.STATUS_RED, 1.0)
	_stop_btn.expand_icon = false
	_stop_btn.add_theme_constant_override("h_separation", 5)
	_stop_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	UITokens.apply_button_states(
		_stop_btn,
		UITokens.STATUS_RED_BG,
		UITokens.STATUS_RED.darkened(0.45),
		UITokens.STATUS_RED_BG,
		Color(0.9725, 0.4431, 0.4431, 0.35),
		UITokens.RADIUS_PILL,
		UITokens.STATUS_RED,
		12,
		4,
		UITokens.TEXT_PRIMARY
	)
	_stop_btn.visible = false
	_stop_btn.pressed.connect(func() -> void: cancel_requested.emit(_current_turn_id))
	_controls_bar.add_child(_stop_btn)


func _process(delta: float) -> void:
	if _bubble_panel.visible and _bubble_fade_timer > 0.0:
		_bubble_fade_timer -= delta
		if _bubble_fade_timer <= 0.0:
			_bubble_panel.visible = false
			queue_redraw()
			_emit_rects()


func on_turn_start(turn_id: String) -> void:
	_current_turn_id = turn_id
	_in_turn = true
	_current_spoken = ""
	_bubble_fade_timer = 0.0
	_bubble_label.text = ""
	_bubble_panel.visible = false
	_stop_btn.visible = true
	_controls_bar.visible = not _dock_tucked
	queue_redraw()
	_emit_rects()


func on_text_delta(delta: String, is_final: bool) -> void:
	if delta != "":
		_current_spoken += delta
		var trimmed := _current_spoken.strip_edges()
		if trimmed.length() > 180:
			trimmed = "…" + trimmed.right(176)
		_show_bubble_text(trimmed, 7.0 if is_final else 0.0)
	elif is_final and _bubble_panel.visible:
		_bubble_fade_timer = 6.5


func on_turn_end() -> void:
	_in_turn = false
	_stop_btn.visible = false
	_controls_bar.visible = false
	if _bubble_panel.visible:
		_bubble_fade_timer = 6.5
	_emit_rects()


func show_transient_status(text: String, fade_sec: float = 4.5) -> void:
	var cleaned := text.strip_edges()
	if cleaned == "":
		return
	_show_bubble_text(cleaned, fade_sec)


func set_avatar_scale(sc: float) -> void:
	_avatar_scale = clampf(sc, 0.50, 1.30)
	_reposition_stack()
	_reposition_bubble()


func _reposition_stack() -> void:
	if _stack == null:
		return
	var stack_x := (VIEWPORT_WIDTH - STACK_WIDTH) * 0.5
	var stack_y := clampf(AVATAR_NECK_Y + 104.0 * _avatar_scale, 332.0, 424.0)
	_stack.position = Vector2(stack_x, stack_y)
	_emit_rects()


func _show_bubble_text(text: String, fade_sec: float) -> void:
	var width := BUBBLE_MAX_W
	if text.find("\n") == -1:
		var font := _bubble_label.get_theme_font("normal_font")
		if font != null:
			var single_w := font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, UITokens.FONT_SIZE_BODY).x
			width = clampf(single_w + 36.0, BUBBLE_MIN_W, BUBBLE_MAX_W)
	_bubble_target_w = width
	_bubble_label.custom_minimum_size = Vector2(width - 24.0, 0.0)
	_bubble_label.text = text
	_bubble_panel.custom_minimum_size = Vector2(width, 0.0)
	_bubble_panel.reset_size()
	_bubble_panel.visible = true
	_bubble_fade_timer = fade_sec
	_reposition_bubble()
	call_deferred("_reposition_bubble")


func _reposition_bubble() -> void:
	if _bubble_panel == null or not _bubble_panel.visible:
		queue_redraw()
		return
	_bubble_panel.reset_size()
	var w := clampf(_bubble_panel.size.x, BUBBLE_MIN_W, BUBBLE_MAX_W)
	var h := clampf(_bubble_panel.size.y, 32.0, 180.0)
	_bubble_panel.size = Vector2(w, h)
	var head_top_y := AVATAR_NECK_Y - 130.0 * _avatar_scale
	var x := (VIEWPORT_WIDTH - w) * 0.5
	var y := clampf(head_top_y - h - 12.0, 8.0, 235.0)
	_bubble_panel.position = Vector2(x, y)
	queue_redraw()
	_collect_and_emit_rects()


func _draw() -> void:
	if _bubble_panel == null or not _bubble_panel.visible or _bubble_panel.size.y <= 1.0:
		return
	var r := _bubble_panel.get_rect()
	var base_y := r.position.y + r.size.y - 0.5
	var tail_x := clampf(AVATAR_CENTER_X, r.position.x + 18.0, r.position.x + r.size.x - 18.0)
	var head_top_y := AVATAR_NECK_Y - 130.0 * _avatar_scale
	var tip_y := maxf(base_y + 8.0, minf(base_y + 12.0, head_top_y + 2.0))
	var p_left := Vector2(tail_x - 6.5, base_y)
	var p_right := Vector2(tail_x + 6.5, base_y)
	var p_tip := Vector2(tail_x, tip_y)
	draw_colored_polygon(PackedVector2Array([p_left, p_right, p_tip]), UITokens.SURFACE_PRIMARY)
	draw_line(p_left, p_tip, UITokens.BORDER_HAIRLINE, 1.0, true)
	draw_line(p_right, p_tip, UITokens.BORDER_HAIRLINE, 1.0, true)


func show_approval_request(approval_id: String, turn_id: String, summary: String) -> void:
	_pending_approval_id = approval_id
	_pending_approval_turn = turn_id
	_approval_label.text = summary
	_approval_panel.visible = true
	_emit_rects()


func _resolve_approval(approved: bool) -> void:
	_approval_panel.visible = false
	_emit_rects()
	if _pending_approval_id != "":
		approval_decided.emit(_pending_approval_id, _pending_approval_turn, approved)
		_pending_approval_id = ""
		_pending_approval_turn = ""


func show_clarification_request(approval_id: String, turn_id: String, question: String, options: Array) -> void:
	_pending_clarify_id = approval_id
	_pending_clarify_turn = turn_id
	_clarify_label.text = question
	_clarify_input.text = ""
	for ch in _clarify_options_box.get_children():
		ch.queue_free()
	var has_opts := false
	for opt_var in options:
		var opt_txt := str(opt_var).strip_edges()
		if opt_txt == "":
			continue
		has_opts = true
		var btn := Button.new()
		btn.text = opt_txt
		btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
		UITokens.apply_button_states(
			btn,
			UITokens.SURFACE_ELEVATED,
			UITokens.SURFACE_HOVER,
			UITokens.SURFACE_PRESSED,
			UITokens.BORDER_HAIRLINE,
			UITokens.RADIUS_BUTTON,
			UITokens.TEXT_PRIMARY,
			9,
			4
		)
		btn.pressed.connect(func() -> void: _resolve_clarification(true, opt_txt))
		_clarify_options_box.add_child(btn)
	_clarify_options_box.visible = has_opts
	_clarify_panel.visible = true
	if get_window() != null:
		get_window().grab_focus()
	_emit_rects()


func _resolve_clarification(approved: bool, answer_text: String) -> void:
	_clarify_panel.visible = false
	_emit_rects()
	if _pending_clarify_id != "":
		clarification_answered.emit(_pending_clarify_id, _pending_clarify_turn, approved, answer_text)
		_pending_clarify_id = ""
		_pending_clarify_turn = ""


func queue_dropped_files(files: PackedStringArray) -> void:
	for f in files:
		if not _dropped_paths.has(f):
			_dropped_paths.append(f)
	if _dropped_paths.is_empty():
		return
	_drop_label.text = "已选 %d 项文件" % _dropped_paths.size()
	_drop_panel.visible = true
	_emit_rects()


func _submit_drop_queue() -> void:
	if _dropped_paths.is_empty():
		return
	var batch: Array[String] = _dropped_paths.duplicate()
	clear_drop_queue()
	drop_queue_submitted.emit(batch)


func clear_drop_queue() -> void:
	_dropped_paths.clear()
	_drop_panel.visible = false
	_emit_rects()


func show_artifact_capsule(title: String, path: String, media_type: String, actions: Array) -> void:
	_artifact_title_btn.text = title
	_artifact_path = path
	_artifact_media_type = media_type
	for ch in _artifact_buttons_box.get_children():
		ch.queue_free()

	var action_meta := {
		"quick_look": {"label": "预览", "icon": "eye", "primary": true},
		"copy_file": {"label": "复制", "icon": "copy", "primary": false},
		"reveal": {"label": "访达", "icon": "folder", "primary": false},
		"pin_image": {"label": "钉住", "icon": "pin", "primary": false},
		"open_external": {"label": "打开", "icon": "external_link", "primary": false},
	}

	var ordered: Array[String] = []
	for act_var in actions:
		var s := str(act_var)
		if s == "open_external" and actions.has("quick_look"):
			continue
		ordered.append(s)

	for act in ordered:
		var meta: Dictionary = action_meta.get(act, {"label": act, "icon": "file", "primary": false})
		var btn := Button.new()
		btn.text = str(meta["label"])
		btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL if bool(meta["primary"]) else Control.SIZE_SHRINK_CENTER
		btn.icon = UITokens.get_icon(str(meta["icon"]), 14, UITokens.TEXT_PRIMARY if bool(meta["primary"]) else UITokens.TEXT_SECONDARY, 1.0)
		btn.expand_icon = false
		btn.add_theme_constant_override("h_separation", 5)
		btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
		if bool(meta["primary"]):
			UITokens.apply_button_states(
				btn,
				UITokens.SURFACE_HOVER,
				UITokens.SURFACE_PRESSED,
				UITokens.ACCENT_CYAN_BG,
				Color(1.0, 1.0, 1.0, 0.16),
				UITokens.RADIUS_BUTTON,
				UITokens.TEXT_PRIMARY,
				10,
				5
			)
		else:
			UITokens.apply_button_states(
				btn,
				UITokens.SURFACE_CHIP,
				UITokens.SURFACE_HOVER,
				UITokens.SURFACE_PRESSED,
				UITokens.BORDER_HAIRLINE,
				UITokens.RADIUS_BUTTON,
				UITokens.TEXT_SECONDARY,
				8,
				5,
				UITokens.TEXT_PRIMARY
			)
		var captured_act := act
		btn.pressed.connect(func() -> void:
			if captured_act == "pin_image":
				open_pin_image_window(_artifact_path)
			else:
				desktop_action_requested.emit(captured_act, _artifact_path, _artifact_media_type)
		)
		_artifact_buttons_box.add_child(btn)

	_artifact_panel.visible = true
	_emit_rects()


func open_pin_image_window(abs_path: String) -> void:
	var win := PIN_WINDOW_SCENE.instantiate() as Window
	if win == null:
		return
	get_tree().root.add_child(win)
	win.call("load_image", abs_path)


func _emit_rects() -> void:
	call_deferred("_collect_and_emit_rects")


func set_dock_tucked(tucked: bool) -> void:
	_dock_tucked = tucked
	if _controls_bar != null:
		_controls_bar.visible = (not tucked) and _in_turn
	_emit_rects()


func _collect_and_emit_rects() -> void:
	var rects: Array[Rect2] = []
	for ctrl in [_bubble_panel, _approval_panel, _clarify_panel, _drop_panel, _artifact_panel, _controls_bar]:
		if ctrl != null and ctrl.visible:
			rects.append(ctrl.get_global_rect())
	var active := (
		_in_turn
		or (_approval_panel != null and _approval_panel.visible)
		or (_clarify_panel != null and _clarify_panel.visible)
		or (_drop_panel != null and _drop_panel.visible)
		or (_artifact_panel != null and _artifact_panel.visible)
	)
	interaction_active_changed.emit(active)
	passthrough_rects_changed.emit(rects)
