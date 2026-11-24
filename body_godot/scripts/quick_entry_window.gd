extends Window

signal prompt_submitted(text: String, with_screen: bool, attached_paths: Array[String])
signal profile_selected(profile_name: String)
signal new_chat_requested
signal visibility_changed_custom(is_visible: bool)

const UITokens := preload("res://theme/ui_tokens.gd")

const SHADOW_MARGIN_X := 28
const SHADOW_MARGIN_TOP := 20
const SHADOW_MARGIN_BOTTOM := 34

var _outer: MarginContainer
var _card: PanelContainer
var _prompt_edit: TextEdit
var _chip_row: HBoxContainer
var _chip_icon: TextureRect
var _chip_label: Label
var _file_btn: Button
var _screen_btn: Button
var _new_chat_btn: Button
var _profile_opt: OptionButton
var _send_btn: Button
var _file_dialog: FileDialog

var _with_screen: bool = false
var _attached_paths: Array[String] = []
var _profile_names: Array[String] = []
var _is_syncing_profiles: bool = false
var _has_send_content: bool = false
var _anim_tween: Tween

var _dragging: bool = false
var _drag_mouse_start: Vector2i
var _drag_win_start: Vector2i


func _ready() -> void:
	theme = UITokens.build_obsidian_theme()
	_build_ui()
	close_requested.connect(close_quick_entry)
	files_dropped.connect(_on_files_dropped)


func _build_ui() -> void:
	_outer = MarginContainer.new()
	_outer.set_anchors_preset(Control.PRESET_FULL_RECT)
	_outer.add_theme_constant_override("margin_left", SHADOW_MARGIN_X)
	_outer.add_theme_constant_override("margin_right", SHADOW_MARGIN_X)
	_outer.add_theme_constant_override("margin_top", SHADOW_MARGIN_TOP)
	_outer.add_theme_constant_override("margin_bottom", SHADOW_MARGIN_BOTTOM)
	add_child(_outer)

	_card = PanelContainer.new()
	var card_sb := UITokens.make_panel_style(
		UITokens.RADIUS_QUICK_ENTRY,
		UITokens.SURFACE_PRIMARY,
		UITokens.BORDER_HAIRLINE,
		26,
		Vector2(0, 10)
	)
	card_sb.content_margin_left = 18
	card_sb.content_margin_right = 18
	card_sb.content_margin_top = 16
	card_sb.content_margin_bottom = 14
	_card.add_theme_stylebox_override("panel", card_sb)
	_card.gui_input.connect(_on_card_gui_input)
	_outer.add_child(_card)
	UITokens.attach_specular_top(_card, UITokens.RADIUS_QUICK_ENTRY)

	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", 12)
	_card.add_child(col)

	# 1. Top composer row (vector star emblem + borderless TextEdit + clickable ESC keycap)
	var top_row := HBoxContainer.new()
	top_row.add_theme_constant_override("separation", 12)
	col.add_child(top_row)

	var emblem := StarEmblem.new()
	top_row.add_child(emblem)

	_prompt_edit = TextEdit.new()
	_prompt_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_prompt_edit.custom_minimum_size = Vector2(0, 72)
	_prompt_edit.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	_prompt_edit.scroll_fit_content_height = false
	_prompt_edit.placeholder_text = "问启明星任何问题…"
	_prompt_edit.add_theme_stylebox_override("normal", StyleBoxEmpty.new())
	_prompt_edit.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
	_prompt_edit.add_theme_font_size_override("font_size", 15)
	_prompt_edit.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	_prompt_edit.add_theme_color_override("font_placeholder_color", UITokens.TEXT_MUTED)
	_prompt_edit.add_theme_color_override("caret_color", UITokens.ACCENT_CYAN)
	_prompt_edit.text_changed.connect(_refresh_send_button_state)
	_prompt_edit.gui_input.connect(_on_prompt_gui_input)
	top_row.add_child(_prompt_edit)

	var esc_btn := Button.new()
	esc_btn.text = "ESC"
	esc_btn.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
	esc_btn.tooltip_text = "关闭输入框"
	esc_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	UITokens.apply_button_states(
		esc_btn,
		UITokens.KEYCAP_BG,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		UITokens.KEYCAP_BORDER,
		UITokens.RADIUS_KEYCAP,
		UITokens.TEXT_SECONDARY,
		8,
		3,
		UITokens.TEXT_PRIMARY
	)
	esc_btn.pressed.connect(close_quick_entry)
	top_row.add_child(esc_btn)

	# 2. Optional attachment / screen-capture context chip row (no emojis)
	_chip_row = HBoxContainer.new()
	_chip_row.add_theme_constant_override("separation", 8)
	_chip_row.visible = false
	col.add_child(_chip_row)

	var chip_card := PanelContainer.new()
	chip_card.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(
			UITokens.RADIUS_PILL,
			UITokens.ACCENT_CYAN_BG,
			UITokens.ACCENT_CYAN_BORDER,
			10,
			4,
			0
		)
	)
	_chip_row.add_child(chip_card)

	var chip_hbox := HBoxContainer.new()
	chip_hbox.add_theme_constant_override("separation", 6)
	chip_card.add_child(chip_hbox)

	_chip_icon = UITokens.make_icon_rect("paperclip", 14, UITokens.ACCENT_CYAN)
	chip_hbox.add_child(_chip_icon)

	_chip_label = Label.new()
	_chip_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	_chip_label.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	chip_hbox.add_child(_chip_label)

	var clr_chip_btn := Button.new()
	clr_chip_btn.icon = UITokens.get_icon("close", 12, UITokens.TEXT_SECONDARY, 1.0)
	clr_chip_btn.expand_icon = false
	clr_chip_btn.icon_alignment = HORIZONTAL_ALIGNMENT_CENTER
	clr_chip_btn.custom_minimum_size = Vector2(22, 22)
	clr_chip_btn.tooltip_text = "清空挂载"
	UITokens.apply_button_states(
		clr_chip_btn,
		UITokens.SURFACE_CHIP,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		UITokens.BORDER_HAIRLINE,
		9,
		UITokens.TEXT_SECONDARY,
		4,
		4
	)
	clr_chip_btn.pressed.connect(_clear_attachments)
	_chip_row.add_child(clr_chip_btn)

	# 3. Hairline divider
	col.add_child(UITokens.make_divider(0))

	# 4. Bottom toolbar: 3 monochrome ghost pills | spacer | green-dot model pill + circular arrow send
	var bot_row := HBoxContainer.new()
	bot_row.add_theme_constant_override("separation", 8)
	col.add_child(bot_row)

	_file_btn = _make_toolbar_pill("文件", "plus")
	_file_btn.pressed.connect(_open_file_picker)
	bot_row.add_child(_file_btn)

	_screen_btn = _make_toolbar_pill("看屏", "eye")
	_screen_btn.toggle_mode = true
	_screen_btn.toggled.connect(_on_screen_toggled)
	bot_row.add_child(_screen_btn)

	_new_chat_btn = _make_toolbar_pill("新对话", "refresh")
	_new_chat_btn.pressed.connect(_on_new_chat_pressed)
	bot_row.add_child(_new_chat_btn)

	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bot_row.add_child(spacer)

	# Unified model selector pill with emerald status indicator dot
	var model_pill := PanelContainer.new()
	model_pill.add_theme_stylebox_override(
		"panel",
		UITokens.make_button_style(
			UITokens.SURFACE_ELEVATED,
			UITokens.BORDER_HAIRLINE,
			UITokens.RADIUS_PILL,
			10,
			3
		)
	)
	bot_row.add_child(model_pill)

	var model_hbox := HBoxContainer.new()
	model_hbox.add_theme_constant_override("separation", 6)
	model_pill.add_child(model_hbox)

	var dot := StatusDot.new()
	dot.dot_color = UITokens.STATUS_EMERALD
	model_hbox.add_child(dot)

	_profile_opt = OptionButton.new()
	_profile_opt.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	_profile_opt.add_theme_stylebox_override("normal", StyleBoxEmpty.new())
	_profile_opt.add_theme_stylebox_override("hover", StyleBoxEmpty.new())
	_profile_opt.add_theme_stylebox_override("pressed", StyleBoxEmpty.new())
	_profile_opt.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
	_profile_opt.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	_profile_opt.add_theme_color_override("font_hover_color", UITokens.ACCENT_CYAN_HOVER)
	_profile_opt.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	_profile_opt.item_selected.connect(_on_profile_item_selected)
	model_hbox.add_child(_profile_opt)

	_send_btn = Button.new()
	_send_btn.custom_minimum_size = Vector2(32, 32)
	_send_btn.expand_icon = false
	_send_btn.icon_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_send_btn.tooltip_text = "发送 (Enter)"
	_send_btn.pressed.connect(_submit_prompt)
	bot_row.add_child(_send_btn)

	# Native multi-file picker for 文件
	_file_dialog = FileDialog.new()
	_file_dialog.file_mode = FileDialog.FILE_MODE_OPEN_FILES
	_file_dialog.access = FileDialog.ACCESS_FILESYSTEM
	_file_dialog.use_native_dialog = true
	_file_dialog.title = "选择挂载给启明星的文件"
	_file_dialog.files_selected.connect(_on_dialog_files_selected)
	add_child(_file_dialog)

	_refresh_chips()
	_refresh_send_button_state()


func _make_toolbar_pill(label_text: String, icon_name: String, active: bool = false) -> Button:
	var btn := Button.new()
	btn.text = label_text
	btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	btn.add_theme_constant_override("h_separation", 6)
	btn.expand_icon = false
	_apply_pill_style(btn, icon_name, active)
	return btn


func _apply_pill_style(btn: Button, icon_name: String, active: bool) -> void:
	if active:
		btn.icon = UITokens.get_icon(icon_name, 14, UITokens.ACCENT_CYAN, 1.0)
		UITokens.apply_button_states(
			btn,
			UITokens.ACCENT_CYAN_BG,
			UITokens.ACCENT_CYAN_BG.lightened(0.08),
			UITokens.ACCENT_CYAN_BG.lightened(0.15),
			UITokens.ACCENT_CYAN_BORDER,
			UITokens.RADIUS_PILL,
			UITokens.TEXT_PRIMARY,
			12,
			5,
			UITokens.TEXT_PRIMARY
		)
	else:
		btn.icon = UITokens.get_icon(icon_name, 14, UITokens.TEXT_SECONDARY, 1.0)
		UITokens.apply_button_states(
			btn,
			UITokens.SURFACE_CHIP,
			UITokens.SURFACE_HOVER,
			UITokens.SURFACE_PRESSED,
			UITokens.BORDER_HAIRLINE,
			UITokens.RADIUS_PILL,
			UITokens.TEXT_SECONDARY,
			12,
			5,
			UITokens.TEXT_PRIMARY
		)


func open_quick_entry() -> void:
	var usable := DisplayServer.screen_get_usable_rect()
	var win_w := size.x
	var target_x := usable.position.x + int((usable.size.x - win_w) * 0.5)
	var target_y := usable.position.y + int(usable.size.y * 0.22)
	position = Vector2i(target_x, target_y)

	if _anim_tween != null and _anim_tween.is_running():
		_anim_tween.kill()

	visible = true
	visibility_changed_custom.emit(true)
	grab_focus()
	if _prompt_edit != null:
		_prompt_edit.call_deferred("grab_focus")
	call_deferred("_sync_window_height")

	if _outer != null and _card != null:
		_outer.modulate.a = 0.0
		_card.pivot_offset = _card.size * 0.5
		_card.scale = Vector2(0.97, 0.97)
		_anim_tween = create_tween().set_parallel(true)
		_anim_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
		_anim_tween.tween_property(_outer, "modulate:a", 1.0, 0.15)
		_anim_tween.tween_property(_card, "scale", Vector2.ONE, 0.15)


func close_quick_entry() -> void:
	if not visible:
		return
	if _anim_tween != null and _anim_tween.is_running():
		_anim_tween.kill()
	if _outer == null or _card == null:
		visible = false
		visibility_changed_custom.emit(false)
		return
	_card.pivot_offset = _card.size * 0.5
	_anim_tween = create_tween().set_parallel(true)
	_anim_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_anim_tween.tween_property(_outer, "modulate:a", 0.0, 0.11)
	_anim_tween.tween_property(_card, "scale", Vector2(0.97, 0.97), 0.11)
	_anim_tween.finished.connect(func() -> void:
		visible = false
		_outer.modulate.a = 1.0
		_card.scale = Vector2.ONE
		visibility_changed_custom.emit(false)
	)


func toggle_quick_entry() -> bool:
	if visible:
		close_quick_entry()
		return false
	open_quick_entry()
	return true


func sync_profiles(active_profile: String, profiles: Array) -> void:
	if _profile_opt == null:
		return
	_is_syncing_profiles = true
	_profile_opt.clear()
	_profile_names.clear()
	var sel_idx := 0
	for i in range(profiles.size()):
		var item: Variant = profiles[i]
		if typeof(item) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = item
		var pname := str(d.get("name", ""))
		var label := str(d.get("label", pname))
		if pname == "":
			continue
		var idx := _profile_names.size()
		_profile_names.append(pname)
		_profile_opt.add_item(label, idx)
		if pname == active_profile:
			sel_idx = idx
	if not _profile_names.is_empty():
		_profile_opt.select(sel_idx)
	_is_syncing_profiles = false


func _on_prompt_gui_input(ev: InputEvent) -> void:
	if not (ev is InputEventKey):
		return
	var kev := ev as InputEventKey
	if not kev.pressed or kev.echo:
		return
	if kev.keycode == KEY_ESCAPE:
		close_quick_entry()
		_prompt_edit.accept_event()
		return
	if (
		(kev.keycode == KEY_ENTER or kev.keycode == KEY_KP_ENTER)
		and not kev.shift_pressed
		and not kev.ctrl_pressed
		and not kev.alt_pressed
		and not kev.meta_pressed
	):
		_submit_prompt()
		_prompt_edit.accept_event()


func _on_card_gui_input(ev: InputEvent) -> void:
	if ev is InputEventMouseButton:
		var mb := ev as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.pressed:
				_dragging = true
				_drag_mouse_start = DisplayServer.mouse_get_position()
				_drag_win_start = position
			else:
				_dragging = false
	elif ev is InputEventMouseMotion and _dragging:
		var cur := DisplayServer.mouse_get_position()
		position = _drag_win_start + (cur - _drag_mouse_start)


func _on_screen_toggled(pressed: bool) -> void:
	_with_screen = pressed
	_apply_pill_style(_screen_btn, "eye", pressed)
	_refresh_chips()
	_refresh_send_button_state()


func _open_file_picker() -> void:
	if _file_dialog != null:
		_file_dialog.popup_centered_ratio(0.65)


func _on_dialog_files_selected(paths: PackedStringArray) -> void:
	_add_paths(paths)


func _on_files_dropped(files: PackedStringArray) -> void:
	_add_paths(files)


func _add_paths(files: PackedStringArray) -> void:
	for p in files:
		var s := str(p)
		if s != "" and not _attached_paths.has(s):
			_attached_paths.append(s)
	_refresh_chips()
	_refresh_send_button_state()
	if _prompt_edit != null:
		_prompt_edit.call_deferred("grab_focus")


func _clear_attachments() -> void:
	_with_screen = false
	if _screen_btn != null:
		_screen_btn.set_pressed_no_signal(false)
		_apply_pill_style(_screen_btn, "eye", false)
	_attached_paths.clear()
	_refresh_chips()
	_refresh_send_button_state()


func _refresh_chips() -> void:
	if _chip_row == null or _chip_label == null:
		return
	var parts: Array[String] = []
	if _with_screen:
		parts.append("将附带当前屏幕画面")
	if not _attached_paths.is_empty():
		parts.append("已挂载 %d 项文件" % _attached_paths.size())
	_chip_row.visible = not parts.is_empty()
	if _chip_icon != null:
		var icon_name := "eye" if (_with_screen and _attached_paths.is_empty()) else "paperclip"
		_chip_icon.texture = UITokens.get_icon(icon_name, 14, UITokens.ACCENT_CYAN, 2.0)
	_chip_label.text = " · ".join(parts)
	call_deferred("_sync_window_height")


func _sync_window_height() -> void:
	if _card == null:
		return
	var target_h := int(ceil(_card.get_combined_minimum_size().y)) + SHADOW_MARGIN_TOP + SHADOW_MARGIN_BOTTOM
	target_h = clampi(target_h, 210, 340)
	if size.y != target_h:
		size = Vector2i(size.x, target_h)


func _refresh_send_button_state() -> void:
	if _send_btn == null or _prompt_edit == null:
		return
	var has_content := (
		_prompt_edit.text.strip_edges() != ""
		or _with_screen
		or not _attached_paths.is_empty()
	)
	_has_send_content = has_content
	if has_content:
		_send_btn.icon = UITokens.get_icon("arrow_up", 16, Color(0.05, 0.08, 0.12), 1.0)
		UITokens.apply_button_states(
			_send_btn,
			UITokens.ACCENT_CYAN,
			UITokens.ACCENT_CYAN_HOVER,
			UITokens.ACCENT_CYAN,
			Color.TRANSPARENT,
			14,
			Color(0.05, 0.08, 0.12),
			6,
			6
		)
	else:
		_send_btn.icon = UITokens.get_icon("arrow_up", 16, UITokens.TEXT_SECONDARY, 1.0)
		UITokens.apply_button_states(
			_send_btn,
			UITokens.SURFACE_ELEVATED,
			UITokens.SURFACE_HOVER,
			UITokens.SURFACE_ELEVATED,
			UITokens.BORDER_HAIRLINE,
			14,
			UITokens.TEXT_SECONDARY,
			6,
			6
		)
	_send_btn.disabled = not has_content


func _on_profile_item_selected(idx: int) -> void:
	if _is_syncing_profiles or idx < 0 or idx >= _profile_names.size():
		return
	profile_selected.emit(_profile_names[idx])


func _on_new_chat_pressed() -> void:
	new_chat_requested.emit()
	_prompt_edit.placeholder_text = "已开启新对话，继续输入…"
	if _prompt_edit != null:
		_prompt_edit.call_deferred("grab_focus")


func _submit_prompt() -> void:
	if _prompt_edit == null:
		return
	var text := _prompt_edit.text.strip_edges()
	if text == "" and not _with_screen and _attached_paths.is_empty():
		return
	var use_screen := _with_screen
	var paths: Array[String] = _attached_paths.duplicate()
	_prompt_edit.text = ""
	_clear_attachments()
	close_quick_entry()
	prompt_submitted.emit(text, use_screen, paths)


class StarEmblem extends Control:
	func _init() -> void:
		custom_minimum_size = Vector2(20, 26)
		size_flags_vertical = Control.SIZE_SHRINK_BEGIN
		mouse_filter = Control.MOUSE_FILTER_IGNORE

	func _draw() -> void:
		var c := Vector2(10.0, 14.5)
		var outer_r := 8.5
		var inner_r := 2.6
		var pts := PackedVector2Array([
			c + Vector2(0, -outer_r),
			c + Vector2(inner_r, -inner_r),
			c + Vector2(outer_r, 0),
			c + Vector2(inner_r, inner_r),
			c + Vector2(0, outer_r),
			c + Vector2(-inner_r, inner_r),
			c + Vector2(-outer_r, 0),
			c + Vector2(-inner_r, -inner_r),
		])
		draw_colored_polygon(pts, UITokens.ACCENT_CYAN)


class StatusDot extends Control:
	var dot_color: Color = Color(0.2039, 0.8275, 0.6000, 1.0)

	func _init() -> void:
		custom_minimum_size = Vector2(8, 16)
		size_flags_vertical = Control.SIZE_SHRINK_CENTER
		mouse_filter = Control.MOUSE_FILTER_IGNORE

	func _draw() -> void:
		draw_circle(Vector2(4.0, size.y * 0.5), 3.2, dot_color)
