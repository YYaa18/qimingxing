extends Window

signal prefs_applied(avatar_scale: float, roam_on: bool, earcons_on: bool, ambient_on: bool, auto_hide: bool)
signal scale_previewed(avatar_scale: float)
signal reset_position_requested
signal reload_mcp_requested
signal open_web_requested

const UITokens := preload("res://theme/ui_tokens.gd")
const ToggleSwitch := preload("res://theme/toggle_switch.gd")

var http_base: String = "http://127.0.0.1:18790"

var _http: HTTPRequest
var _probe_http: HTTPRequest
var _mcp_http: HTTPRequest
var _pending_mode: String = "" # "get" | "post"
var _probing_provider_id: String = ""
var _mcp_pending_op: String = ""

var _loaded_settings: Dictionary = {}
var _providers_data: Dictionary = {}
var _provider_order: Array[String] = []
var _routing_data: Dictionary = {}
var _pending_provider_patches: Dictionary = {}
var _is_syncing_ui: bool = false

# Navigation & Right Chrome
var _shell: HBoxContainer
var _tabs: TabContainer
var _nav_buttons: Array[Button] = []
var _nav_meta: Array = []
var _page_title_label: Label
var _page_subtitle_label: Label
var _header_actions_box: HBoxContainer
var _header_add_prov_btn: Button
var _header_add_mcp_btn: Button
var _header_ref_mcp_btn: Button
var _status_label: Label
var _primary_btn: Button

# Tab 0: 外观与交互
var _scale_slider: HSlider
var _scale_val_label: Label
var _roam_sw: Control
var _earcons_sw: Control
var _ambient_sw: Control
var _auto_hide_sw: Control

# Tab 1: 模型与路由
var _routing_scroll: ScrollContainer
var _slot_widgets: Dictionary = {} # slot_key -> {prov_opt, model_edit, preset_menu, voice_opt?}
var _providers_list_box: VBoxContainer
var _prov_editor_card: PanelContainer
var _prov_editor_title: Label
var _prov_id_edit: LineEdit
var _prov_label_edit: LineEdit
var _prov_url_edit: LineEdit
var _prov_key_edit: LineEdit
var _prov_editing_is_new: bool = false

# Tab 2: 语音与隐私
var _audio_enabled_sw: Control
var _privacy_mode_opt: OptionButton
var _inbox_dir_edit: LineEdit

# Tab 3: 快捷键
var _sc_ptt_opt: OptionButton
var _sc_chat_opt: OptionButton
var _sc_cancel_opt: OptionButton
var _sc_dock_opt: OptionButton
var _sc_settings_opt: OptionButton

# Tab 4: MCP 服务
var _mcp_servers: Array = []
var _mcp_rows_box: VBoxContainer
var _mcp_selected_name: String = ""
var _mcp_name_edit: LineEdit
var _mcp_transport_opt: OptionButton
var _mcp_auth_row: HBoxContainer
var _mcp_auth_opt: OptionButton
var _mcp_url_row: HBoxContainer
var _mcp_url_edit: LineEdit
var _mcp_cmd_row: HBoxContainer
var _mcp_cmd_edit: LineEdit
var _mcp_args_row: HBoxContainer
var _mcp_args_edit: LineEdit
var _mcp_safe_tools_edit: LineEdit
var _mcp_enabled_val: bool = true
var _mcp_auth_url_row: HBoxContainer
var _mcp_auth_url_edit: LineEdit
var _mcp_tools_label: Label
var _mcp_poll_timer: Timer
var _mcp_poll_ticks: int = 0

const VOICE_OPTIONS := ["stella", "tide", "alex", "alloy"]
const PRIVACY_OPTIONS := ["off", "meta_only", "full_on_demand"]
const PTT_HOTKEY_PRESETS := ["right_option", "left_option", "option+space", "ctrl+space", "fn", "disabled"]
const CHAT_HOTKEY_PRESETS := ["option+space", "ctrl+space", "option+t", "option+enter", "f2", "disabled"]
const CANCEL_HOTKEY_PRESETS := ["option+esc", "ctrl+esc", "option+x", "f4", "disabled"]
const DOCK_HOTKEY_PRESETS := ["option+h", "option+d", "ctrl+option+h", "f3", "disabled"]
const SETTINGS_HOTKEY_PRESETS := ["option+k", "option+s", "ctrl+option+s", "disabled"]

const SLOT_SPECS := [
	{"key": "chat", "label": "主对话 (Chat)", "icon": "chat"},
	{"key": "vision_ocr", "label": "视觉与 OCR", "icon": "eye"},
	{"key": "fast_utility", "label": "后台快模型", "icon": "zap"},
	{"key": "asr", "label": "语音识别 (ASR)", "icon": "mic"},
	{"key": "tts", "label": "语音合成 (TTS)", "icon": "audio_wave"},
]

const PAGE_HEADERS := [
	{"title": "外观与交互", "sub": "自定义桌面真身缩放、自由漫步与声音反馈"},
	{"title": "模型与路由", "sub": "自由搭配各服务商模型，为对话、看屏与语音独立分配最优引擎"},
	{"title": "语音与隐私", "sub": "管理语音闭环开关、桌面感知隐私边界与文件收件箱"},
	{"title": "快捷键", "sub": "设置全局热键组合，保存后立即热更新生效"},
	{"title": "MCP 服务", "sub": "管理外部 MCP 工具（HTTP / SSE / stdio）与 OAuth 授权"},
]


func _ready() -> void:
	theme = UITokens.build_obsidian_theme()
	_http = HTTPRequest.new()
	add_child(_http)
	_http.request_completed.connect(_on_http_completed)

	_probe_http = HTTPRequest.new()
	add_child(_probe_http)
	_probe_http.request_completed.connect(_on_probe_http_completed)

	_mcp_http = HTTPRequest.new()
	add_child(_mcp_http)
	_mcp_http.request_completed.connect(_on_mcp_http_completed)

	_mcp_poll_timer = Timer.new()
	_mcp_poll_timer.wait_time = 2.0
	_mcp_poll_timer.one_shot = false
	_mcp_poll_timer.timeout.connect(_on_mcp_poll_timeout)
	add_child(_mcp_poll_timer)

	close_requested.connect(func() -> void:
		_mcp_poll_timer.stop()
		visible = false
	)
	size_changed.connect(_sync_shell_layout)
	_build_ui()


func _sync_shell_layout() -> void:
	if _shell != null:
		_shell.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)


func open_settings(
	cur_http_base: String,
	cur_scale: float,
	cur_roam: bool,
	cur_earcons: bool,
	cur_ambient: bool,
	initial_tab: int = 0
) -> void:
	http_base = cur_http_base.trim_suffix("/")
	min_size = Vector2i(820, 560)
	var target_size := Vector2i(maxi(size.x, 880), maxi(size.y, 620))
	size = target_size
	var usable := DisplayServer.screen_get_usable_rect()
	position = Vector2i(
		usable.position.x + maxi(0, (usable.size.x - target_size.x) / 2),
		usable.position.y + maxi(0, (usable.size.y - target_size.y) / 2)
	)
	_is_syncing_ui = true
	_scale_slider.value = cur_scale * 100.0
	_scale_val_label.text = "%d%%" % int(round(cur_scale * 100.0))
	_roam_sw.call("set_checked", cur_roam, false)
	_earcons_sw.call("set_checked", cur_earcons, false)
	_ambient_sw.call("set_checked", cur_ambient, false)
	_pending_provider_patches.clear()
	if _prov_editor_card != null:
		_prov_editor_card.visible = false
	_switch_page(clampi(initial_tab, 0, PAGE_HEADERS.size() - 1))
	_is_syncing_ui = false

	visible = true
	call_deferred("_sync_shell_layout")
	grab_focus()
	_fetch_backend_settings()
	_fetch_mcp_servers()


func _switch_page(tab_idx: int) -> void:
	var idx := clampi(tab_idx, 0, PAGE_HEADERS.size() - 1)
	if _tabs != null:
		_tabs.current_tab = idx
	_sync_nav_buttons(idx)
	var meta: Dictionary = PAGE_HEADERS[idx]
	if _page_title_label != null:
		_page_title_label.text = str(meta["title"])
	if _page_subtitle_label != null:
		_page_subtitle_label.text = str(meta["sub"])
	if _header_add_prov_btn != null:
		_header_add_prov_btn.visible = (idx == 1)
	if _header_add_mcp_btn != null:
		_header_add_mcp_btn.visible = (idx == 4)
	if _header_ref_mcp_btn != null:
		_header_ref_mcp_btn.visible = (idx == 4)
	if _primary_btn != null:
		_primary_btn.text = "保存并连接" if idx == 4 else "保存并生效"


func _sync_nav_buttons(active_idx: int) -> void:
	for i in range(_nav_buttons.size()):
		var btn := _nav_buttons[i]
		var icon_name := str(_nav_meta[i].get("icon", "settings"))
		var is_active := i == active_idx
		if is_active:
			btn.icon = UITokens.get_icon(icon_name, 15, UITokens.ACCENT_CYAN, 1.0)
			UITokens.apply_button_states(
				btn,
				UITokens.SURFACE_ELEVATED,
				UITokens.SURFACE_HOVER,
				UITokens.SURFACE_PRESSED,
				UITokens.BORDER_HAIRLINE,
				9,
				UITokens.TEXT_PRIMARY,
				12,
				8
			)
		else:
			btn.icon = UITokens.get_icon(icon_name, 15, UITokens.TEXT_SECONDARY, 1.0)
			UITokens.apply_button_states(
				btn,
				Color.TRANSPARENT,
				UITokens.SURFACE_GHOST_HOVER,
				UITokens.SURFACE_HOVER,
				Color.TRANSPARENT,
				9,
				UITokens.TEXT_SECONDARY,
				12,
				8,
				UITokens.TEXT_PRIMARY
			)


func _make_card(parent: Control, sep: int = 10, pad_x: int = 14, pad_y: int = 12) -> VBoxContainer:
	var card := PanelContainer.new()
	card.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(12, UITokens.SURFACE_CARD, UITokens.BORDER_HAIRLINE, pad_x, pad_y, 0)
	)
	parent.add_child(card)
	var col := VBoxContainer.new()
	col.add_theme_constant_override("separation", sep)
	card.add_child(col)
	return col


func _make_card_header(parent: Control, title_txt: String, right_ctrl: Control = null) -> HBoxContainer:
	var hbox := HBoxContainer.new()
	hbox.add_theme_constant_override("separation", 8)
	parent.add_child(hbox)
	var lbl := Label.new()
	lbl.text = title_txt
	lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	lbl.add_theme_font_size_override("font_size", 13)
	lbl.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	hbox.add_child(lbl)
	if right_ctrl != null:
		hbox.add_child(right_ctrl)
	return hbox


func _make_labeled_row(parent: Control, label_text: String, ctrl: Control, label_w: int = 108) -> HBoxContainer:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 10)
	parent.add_child(row)
	var lbl := Label.new()
	lbl.text = label_text
	lbl.custom_minimum_size = Vector2(label_w, 0)
	lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
	lbl.add_theme_color_override("font_color", UITokens.TEXT_SECONDARY)
	row.add_child(lbl)
	ctrl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(ctrl)
	return row


func _add_toggle_row(parent: Control, label_text: String, initial: bool = false) -> Control:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 10)
	parent.add_child(row)
	var lbl := Label.new()
	lbl.text = label_text
	lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
	lbl.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	row.add_child(lbl)
	var sw: Control = ToggleSwitch.new()
	sw.checked = initial
	row.add_child(sw)
	return sw


func _add_hint_label(parent: Control, txt: String) -> Label:
	var lbl := Label.new()
	lbl.text = txt
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	lbl.add_theme_color_override("font_color", UITokens.TEXT_MUTED)
	parent.add_child(lbl)
	return lbl


func _style_secondary_btn(btn: Button, icon_name: String = "") -> void:
	if icon_name != "":
		btn.icon = UITokens.get_icon(icon_name, 14, UITokens.TEXT_SECONDARY, 1.0)
		btn.expand_icon = false
		btn.add_theme_constant_override("h_separation", 6)
	btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	UITokens.apply_button_states(
		btn,
		UITokens.SURFACE_ELEVATED,
		UITokens.SURFACE_HOVER,
		UITokens.SURFACE_PRESSED,
		UITokens.BORDER_HAIRLINE,
		UITokens.RADIUS_BUTTON,
		UITokens.TEXT_PRIMARY,
		12,
		6
	)


func _style_primary_btn(btn: Button, icon_name: String = "") -> void:
	if icon_name != "":
		btn.icon = UITokens.get_icon(icon_name, 14, Color(0.05, 0.08, 0.12), 1.0)
		btn.expand_icon = false
		btn.add_theme_constant_override("h_separation", 6)
	btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	UITokens.apply_button_states(
		btn,
		UITokens.ACCENT_CYAN,
		UITokens.ACCENT_CYAN_HOVER,
		UITokens.ACCENT_CYAN,
		Color.TRANSPARENT,
		UITokens.RADIUS_BUTTON,
		Color(0.05, 0.08, 0.12),
		14,
		6,
		Color(0.05, 0.08, 0.12)
	)


func _style_slider(slider: HSlider) -> void:
	var rail := StyleBoxFlat.new()
	rail.bg_color = UITokens.SWITCH_OFF
	rail.set_corner_radius_all(3)
	rail.content_margin_top = 3
	rail.content_margin_bottom = 3
	var fill := StyleBoxFlat.new()
	fill.bg_color = UITokens.ACCENT_CYAN
	fill.set_corner_radius_all(3)
	fill.content_margin_top = 3
	fill.content_margin_bottom = 3
	slider.add_theme_stylebox_override("slider", rail)
	slider.add_theme_stylebox_override("grabber_area", fill)
	slider.add_theme_stylebox_override("grabber_area_highlight", fill)


func _build_ui() -> void:
	var shell := HBoxContainer.new()
	_shell = shell
	shell.set_anchors_preset(Control.PRESET_FULL_RECT)
	shell.add_theme_constant_override("separation", 0)
	add_child(shell)

	# --- Left Sidebar (196px fixed, SURFACE_SIDEBAR) ---
	var sidebar := PanelContainer.new()
	sidebar.custom_minimum_size = Vector2(196, 0)
	var sb_side := StyleBoxFlat.new()
	sb_side.bg_color = Color(0.0549, 0.0667, 0.0941, 1.0)
	sb_side.content_margin_left = 12
	sb_side.content_margin_right = 12
	sb_side.content_margin_top = 18
	sb_side.content_margin_bottom = 14
	sidebar.add_theme_stylebox_override("panel", sb_side)
	shell.add_child(sidebar)

	var side_col := VBoxContainer.new()
	side_col.add_theme_constant_override("separation", 6)
	sidebar.add_child(side_col)

	var brand_row := HBoxContainer.new()
	brand_row.add_theme_constant_override("separation", 8)
	side_col.add_child(brand_row)
	brand_row.add_child(StarEmblem.new())
	var brand_lbl := Label.new()
	brand_lbl.text = "启明星 设置"
	brand_lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	brand_lbl.add_theme_font_size_override("font_size", 15)
	brand_lbl.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	brand_row.add_child(brand_lbl)

	var nav_spacer_top := Control.new()
	nav_spacer_top.custom_minimum_size = Vector2(0, 10)
	side_col.add_child(nav_spacer_top)

	_nav_meta = [
		{"title": "外观交互", "icon": "palette"},
		{"title": "模型与路由", "icon": "routing"},
		{"title": "语音隐私", "icon": "mic"},
		{"title": "快捷键", "icon": "keyboard"},
		{"title": "MCP 服务", "icon": "box"},
	]
	_nav_buttons.clear()
	for i in range(_nav_meta.size()):
		var nb := Button.new()
		nb.text = str(_nav_meta[i]["title"])
		nb.alignment = HORIZONTAL_ALIGNMENT_LEFT
		nb.expand_icon = false
		nb.add_theme_constant_override("h_separation", 10)
		nb.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
		var tab_i := i
		nb.pressed.connect(func() -> void: _switch_page(tab_i))
		side_col.add_child(nb)
		_nav_buttons.append(nb)

	var side_fill := Control.new()
	side_fill.size_flags_vertical = Control.SIZE_EXPAND_FILL
	side_col.add_child(side_fill)

	var ver_lbl := Label.new()
	ver_lbl.text = "MVP 7.0 Obsidian"
	ver_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	ver_lbl.add_theme_color_override("font_color", UITokens.TEXT_MUTED)
	side_col.add_child(ver_lbl)

	# Vertical Hairline Divider
	var vdiv := ColorRect.new()
	vdiv.color = UITokens.DIVIDER
	vdiv.custom_minimum_size = Vector2(1, 0)
	shell.add_child(vdiv)

	# --- Right Main Content Area ---
	var main_bg := PanelContainer.new()
	main_bg.clip_contents = true
	main_bg.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var sb_main := StyleBoxFlat.new()
	sb_main.bg_color = Color(0.0784, 0.0941, 0.1294, 1.0)
	sb_main.content_margin_left = 0
	sb_main.content_margin_right = 0
	sb_main.content_margin_top = 0
	sb_main.content_margin_bottom = 0
	main_bg.add_theme_stylebox_override("panel", sb_main)
	shell.add_child(main_bg)

	var right_col := VBoxContainer.new()
	right_col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	right_col.add_theme_constant_override("separation", 0)
	main_bg.add_child(right_col)

	# Page Header
	var hdr_wrap := MarginContainer.new()
	hdr_wrap.add_theme_constant_override("margin_left", 22)
	hdr_wrap.add_theme_constant_override("margin_right", 22)
	hdr_wrap.add_theme_constant_override("margin_top", 13)
	hdr_wrap.add_theme_constant_override("margin_bottom", 8)
	right_col.add_child(hdr_wrap)

	var hdr_row := HBoxContainer.new()
	hdr_row.add_theme_constant_override("separation", 10)
	hdr_wrap.add_child(hdr_row)

	var hdr_txt_col := VBoxContainer.new()
	hdr_txt_col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hdr_txt_col.add_theme_constant_override("separation", 3)
	hdr_row.add_child(hdr_txt_col)

	_page_title_label = Label.new()
	_page_title_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_PAGE_TITLE)
	_page_title_label.add_theme_color_override("font_color", UITokens.TEXT_PRIMARY)
	hdr_txt_col.add_child(_page_title_label)

	_page_subtitle_label = Label.new()
	_page_subtitle_label.clip_text = true
	_page_subtitle_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_page_subtitle_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	_page_subtitle_label.add_theme_color_override("font_color", UITokens.TEXT_SECONDARY)
	hdr_txt_col.add_child(_page_subtitle_label)

	_header_actions_box = HBoxContainer.new()
	_header_actions_box.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	_header_actions_box.add_theme_constant_override("separation", 6)
	hdr_row.add_child(_header_actions_box)

	_header_add_prov_btn = UITokens.make_ghost_button("添加服务商", "plus", 14)
	_header_add_prov_btn.pressed.connect(_open_new_provider_editor)
	_header_actions_box.add_child(_header_add_prov_btn)

	_header_add_mcp_btn = UITokens.make_ghost_button("添加服务", "plus", 14)
	_header_add_mcp_btn.pressed.connect(_clear_mcp_editor)
	_header_actions_box.add_child(_header_add_mcp_btn)

	_header_ref_mcp_btn = UITokens.make_ghost_button("刷新", "refresh", 14)
	_header_ref_mcp_btn.pressed.connect(_fetch_mcp_servers)
	_header_actions_box.add_child(_header_ref_mcp_btn)

	# Scrollable Tab Body
	_tabs = TabContainer.new()
	_tabs.tabs_visible = false
	_tabs.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_tabs.add_theme_stylebox_override("panel", StyleBoxEmpty.new())
	right_col.add_child(_tabs)

	_build_tab_appearance()
	_build_tab_routing()
	_build_tab_voice_privacy()
	_build_tab_shortcuts()
	_build_tab_mcp()

	right_col.add_child(UITokens.make_divider())

	# Bottom Footer (52px)
	var foot_wrap := MarginContainer.new()
	foot_wrap.custom_minimum_size = Vector2(0, 52)
	foot_wrap.add_theme_constant_override("margin_left", 22)
	foot_wrap.add_theme_constant_override("margin_right", 22)
	foot_wrap.add_theme_constant_override("margin_top", 8)
	foot_wrap.add_theme_constant_override("margin_bottom", 8)
	right_col.add_child(foot_wrap)

	var footer := HBoxContainer.new()
	footer.add_theme_constant_override("separation", 8)
	foot_wrap.add_child(footer)

	footer.add_child(UITokens.make_icon_rect("lock", 13, UITokens.TEXT_MUTED))

	_status_label = Label.new()
	_status_label.text = "本地 0600 安全存储（密钥已脱敏）"
	_status_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_status_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_status_label.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
	_status_label.add_theme_color_override("font_color", UITokens.TEXT_SECONDARY)
	footer.add_child(_status_label)

	var close_btn := Button.new()
	close_btn.text = "关闭"
	_style_secondary_btn(close_btn)
	close_btn.pressed.connect(func() -> void: visible = false)
	footer.add_child(close_btn)

	_primary_btn = Button.new()
	_primary_btn.text = "保存并生效"
	_style_primary_btn(_primary_btn, "check")
	_primary_btn.pressed.connect(_on_primary_action_pressed)
	footer.add_child(_primary_btn)

	_switch_page(0)


func _make_page_scroll(tab_name: String) -> VBoxContainer:
	var scroll := ScrollContainer.new()
	scroll.name = tab_name
	scroll.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	_tabs.add_child(scroll)

	var pad := MarginContainer.new()
	pad.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	pad.add_theme_constant_override("margin_left", 22)
	pad.add_theme_constant_override("margin_right", 22)
	pad.add_theme_constant_override("margin_top", 4)
	pad.add_theme_constant_override("margin_bottom", 14)
	scroll.add_child(pad)

	var col := VBoxContainer.new()
	col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	col.add_theme_constant_override("separation", 12)
	pad.add_child(col)
	return col


func _build_tab_appearance() -> void:
	var t1 := _make_page_scroll("外观交互")
	var card := _make_card(t1, 10)

	var scale_row := HBoxContainer.new()
	scale_row.add_theme_constant_override("separation", 10)
	card.add_child(scale_row)

	var sl_lbl := Label.new()
	sl_lbl.text = "角色缩放"
	sl_lbl.custom_minimum_size = Vector2(108, 0)
	sl_lbl.add_theme_color_override("font_color", UITokens.TEXT_SECONDARY)
	scale_row.add_child(sl_lbl)

	_scale_slider = HSlider.new()
	_scale_slider.min_value = 50.0
	_scale_slider.max_value = 130.0
	_scale_slider.step = 2.0
	_scale_slider.value = 78.0
	_scale_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_style_slider(_scale_slider)
	_scale_slider.value_changed.connect(func(v: float) -> void:
		_scale_val_label.text = "%d%%" % int(round(v))
		if not _is_syncing_ui:
			scale_previewed.emit(v / 100.0)
	)
	scale_row.add_child(_scale_slider)

	_scale_val_label = Label.new()
	_scale_val_label.custom_minimum_size = Vector2(44, 0)
	_scale_val_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_scale_val_label.text = "78%"
	_scale_val_label.add_theme_color_override("font_color", UITokens.ACCENT_CYAN)
	scale_row.add_child(_scale_val_label)

	var reset_pos_btn := UITokens.make_ghost_button("重置位置", "reset", 13)
	reset_pos_btn.pressed.connect(func() -> void: reset_position_requested.emit())
	scale_row.add_child(reset_pos_btn)

	card.add_child(UITokens.make_divider())
	_roam_sw = _add_toggle_row(card, "桌面自由漫步（闲置时轻盈巡游桌面）", true)
	card.add_child(UITokens.make_divider())
	_earcons_sw = _add_toggle_row(card, "状态提醒微音效 (Earcons)", true)
	card.add_child(UITokens.make_divider())
	_ambient_sw = _add_toggle_row(card, "柔和环境氛围音（播报自动压低）", false)
	card.add_child(UITokens.make_divider())
	_auto_hide_sw = _add_toggle_row(card, "截屏看屏前自动短暂隐身防自拍", true)

	_add_hint_label(t1, "提示：将启明星拖至屏幕左 / 右 / 上边缘松手即可贴边隐藏，鼠标靠近边缘自动滑出；滚轮可实时缩放。")


func _build_tab_routing() -> void:
	var t2 := _make_page_scroll("模型与路由")
	_routing_scroll = t2.get_parent().get_parent() as ScrollContainer
	t2.add_theme_constant_override("separation", 7)

	# Upper Card: 5-Slot Capability Routing
	var route_card := _make_card(t2, 5, 12, 8)
	_make_card_header(route_card, "能力路由分配")
	route_card.add_child(UITokens.make_divider())

	_slot_widgets.clear()
	for idx in range(SLOT_SPECS.size()):
		var spec: Dictionary = SLOT_SPECS[idx]
		var slot_key := str(spec["key"])
		if idx > 0:
			route_card.add_child(UITokens.make_divider())

		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation", 6)
		route_card.add_child(row)

		row.add_child(UITokens.make_icon_rect(str(spec["icon"]), 14, UITokens.TEXT_SECONDARY))

		var slot_lbl := Label.new()
		slot_lbl.text = str(spec["label"])
		slot_lbl.custom_minimum_size = Vector2(96, 0)
		slot_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
		row.add_child(slot_lbl)

		var prov_opt := OptionButton.new()
		prov_opt.custom_minimum_size = Vector2(98, 0)
		prov_opt.clip_text = true
		prov_opt.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
		row.add_child(prov_opt)

		var plus_lbl := Label.new()
		plus_lbl.text = "+"
		plus_lbl.add_theme_color_override("font_color", UITokens.TEXT_MUTED)
		row.add_child(plus_lbl)

		# Editable Model Combo (manual input LineEdit + preset MenuButton)
		var model_wrap := PanelContainer.new()
		model_wrap.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		model_wrap.add_theme_stylebox_override(
			"panel",
			UITokens.make_button_style(UITokens.SURFACE_ELEVATED, UITokens.BORDER_HAIRLINE, 8, 8, 3)
		)
		row.add_child(model_wrap)

		var m_hbox := HBoxContainer.new()
		m_hbox.add_theme_constant_override("separation", 4)
		model_wrap.add_child(m_hbox)

		var model_edit := LineEdit.new()
		model_edit.placeholder_text = "选择或输入模型 ID"
		model_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		model_edit.add_theme_stylebox_override("normal", StyleBoxEmpty.new())
		model_edit.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
		model_edit.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
		m_hbox.add_child(model_edit)

		var preset_menu := MenuButton.new()
		preset_menu.custom_minimum_size = Vector2(22, 22)
		preset_menu.icon = UITokens.get_icon("chevron_down", 12, UITokens.TEXT_SECONDARY, 1.0)
		preset_menu.flat = true
		preset_menu.tooltip_text = "选择推荐模型预设"
		preset_menu.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
		m_hbox.add_child(preset_menu)

		var voice_opt: OptionButton = null
		if slot_key == "tts":
			var plus2 := Label.new()
			plus2.text = "+"
			plus2.add_theme_color_override("font_color", UITokens.TEXT_MUTED)
			row.add_child(plus2)
			voice_opt = OptionButton.new()
			voice_opt.custom_minimum_size = Vector2(74, 0)
			voice_opt.clip_text = true
			voice_opt.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
			for v_i in range(VOICE_OPTIONS.size()):
				voice_opt.add_item(VOICE_OPTIONS[v_i], v_i)
			row.add_child(voice_opt)

		var sk := slot_key
		prov_opt.item_selected.connect(func(sel_i: int) -> void:
			_on_slot_provider_changed(sk, sel_i)
		)
		preset_menu.get_popup().index_pressed.connect(func(p_i: int) -> void:
			var p_txt := preset_menu.get_popup().get_item_text(p_i)
			if p_txt != "":
				model_edit.text = p_txt
		)

		_slot_widgets[slot_key] = {
			"prov_opt": prov_opt,
			"model_edit": model_edit,
			"preset_menu": preset_menu,
			"voice_opt": voice_opt,
		}

	# Lower Card: Providers & Key Pool
	var prov_card := _make_card(t2, 5, 12, 8)
	_make_card_header(prov_card, "服务商与密钥池")
	prov_card.add_child(UITokens.make_divider())

	# Inline Collapsible Provider Editor (placed at top of card for immediate visibility)
	_prov_editor_card = PanelContainer.new()
	_prov_editor_card.visible = false
	_prov_editor_card.add_theme_stylebox_override(
		"panel",
		UITokens.make_capsule_style(10, UITokens.SURFACE_ELEVATED, UITokens.ACCENT_CYAN_BORDER, 12, 10, 0)
	)
	prov_card.add_child(_prov_editor_card)

	var pe_col := VBoxContainer.new()
	pe_col.add_theme_constant_override("separation", 8)
	_prov_editor_card.add_child(pe_col)

	_prov_editor_title = Label.new()
	_prov_editor_title.add_theme_font_size_override("font_size", 13)
	_prov_editor_title.add_theme_color_override("font_color", UITokens.ACCENT_CYAN)
	pe_col.add_child(_prov_editor_title)

	_prov_id_edit = LineEdit.new()
	_prov_id_edit.placeholder_text = "英文标识（如 deepseek / my_proxy）"
	_make_labeled_row(pe_col, "服务商标识", _prov_id_edit)

	_prov_label_edit = LineEdit.new()
	_prov_label_edit.placeholder_text = "显示名称（如 DeepSeek / 硅基流动）"
	_make_labeled_row(pe_col, "显示名称", _prov_label_edit)

	_prov_url_edit = LineEdit.new()
	_prov_url_edit.placeholder_text = "OpenAI 兼容 Base URL（如 https://api.siliconflow.cn/v1）"
	_make_labeled_row(pe_col, "接口地址", _prov_url_edit)

	var sec_res := UITokens.make_secret_input("留空则保留当前已保存密钥")
	_prov_key_edit = sec_res["edit"]
	_make_labeled_row(pe_col, "API Key", sec_res["container"])

	var pe_actions := HBoxContainer.new()
	pe_actions.alignment = BoxContainer.ALIGNMENT_END
	pe_actions.add_theme_constant_override("separation", 8)
	pe_col.add_child(pe_actions)

	var pe_test := Button.new()
	pe_test.text = "测试连通"
	_style_secondary_btn(pe_test, "plug")
	pe_test.pressed.connect(func() -> void:
		_commit_provider_editor_patch()
		var pid := _prov_id_edit.text.strip_edges()
		if pid != "":
			_test_provider_connectivity(pid)
	)
	pe_actions.add_child(pe_test)

	var pe_done := UITokens.make_ghost_button("完成编辑", "check", 13)
	pe_done.pressed.connect(func() -> void:
		_commit_provider_editor_patch()
		_prov_editor_card.visible = false
	)
	pe_actions.add_child(pe_done)

	_providers_list_box = VBoxContainer.new()
	_providers_list_box.add_theme_constant_override("separation", 4)
	prov_card.add_child(_providers_list_box)


func _build_tab_voice_privacy() -> void:
	var t3 := _make_page_scroll("语音隐私")
	var c3 := _make_card(t3, 10)

	_audio_enabled_sw = _add_toggle_row(c3, "启用流式语音识别 (ASR) 与语音合成 (TTS)", true)
	c3.add_child(UITokens.make_divider())

	_privacy_mode_opt = OptionButton.new()
	_privacy_mode_opt.custom_minimum_size = Vector2(180, 0)
	_privacy_mode_opt.clip_text = true
	_privacy_mode_opt.add_item("off（完全关闭桌面感知）", 0)
	_privacy_mode_opt.add_item("meta_only（仅窗口标题，推荐）", 1)
	_privacy_mode_opt.add_item("full_on_demand（按需截屏看屏）", 2)
	_make_labeled_row(c3, "默认隐私边界", _privacy_mode_opt)

	c3.add_child(UITokens.make_divider())

	_inbox_dir_edit = LineEdit.new()
	_inbox_dir_edit.placeholder_text = "~/Desktop/给启明星"
	_make_labeled_row(c3, "收件箱目录", _inbox_dir_edit)

	_add_hint_label(t3, "说明：语音引擎与音色选择已统一合并至「模型与路由」页面中的 ASR / TTS 槽位。")

	var util_row := HBoxContainer.new()
	util_row.alignment = BoxContainer.ALIGNMENT_END
	util_row.add_theme_constant_override("separation", 8)
	t3.add_child(util_row)

	var mcp_btn := UITokens.make_ghost_button("热重载 MCP 工具", "refresh", 13)
	mcp_btn.pressed.connect(func() -> void: reload_mcp_requested.emit())
	util_row.add_child(mcp_btn)

	var web_btn := UITokens.make_ghost_button("打开 Web 调试台", "external_link", 13)
	web_btn.pressed.connect(func() -> void: open_web_requested.emit())
	util_row.add_child(web_btn)


func _build_tab_shortcuts() -> void:
	var t4 := _make_page_scroll("快捷键")
	var c4 := _make_card(t4, 8)

	_sc_ptt_opt = _add_hotkey_row(c4, "mic", "麦克风按住说话 (PTT)", PTT_HOTKEY_PRESETS)
	c4.add_child(UITokens.make_divider())
	_sc_chat_opt = _add_hotkey_row(c4, "chat", "文字输入框呼出/收起", CHAT_HOTKEY_PRESETS)
	c4.add_child(UITokens.make_divider())
	_sc_cancel_opt = _add_hotkey_row(c4, "stop", "急停播报与任务", CANCEL_HOTKEY_PRESETS)
	c4.add_child(UITokens.make_divider())
	_sc_dock_opt = _add_hotkey_row(c4, "roaming", "贴边隐藏 / 呼出助手", DOCK_HOTKEY_PRESETS)
	c4.add_child(UITokens.make_divider())
	_sc_settings_opt = _add_hotkey_row(c4, "keyboard", "打开快捷键与设置", SETTINGS_HOTKEY_PRESETS)

	var sc_ops := HBoxContainer.new()
	sc_ops.alignment = BoxContainer.ALIGNMENT_END
	t4.add_child(sc_ops)
	var reset_sc_btn := UITokens.make_ghost_button("恢复默认快捷键", "reset", 13)
	reset_sc_btn.pressed.connect(_reset_shortcut_defaults)
	sc_ops.add_child(reset_sc_btn)

	var hint_card := _make_card(t4, 4, 14, 10)
	_add_hint_label(hint_card, "• 无麦克风时可将 PTT 设为 disabled，使用「option+space」随处呼出文字输入框。")
	_add_hint_label(hint_card, "• 全局热键需在「系统设置 → 隐私与安全性 → 辅助功能 / 输入监控」允许终端或 Python。")


func _add_hotkey_row(parent: Control, icon_name: String, label_text: String, presets: Array) -> OptionButton:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 8)
	parent.add_child(row)

	row.add_child(UITokens.make_icon_rect(icon_name, 14, UITokens.TEXT_SECONDARY))

	var lbl := Label.new()
	lbl.text = label_text
	lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(lbl)

	var opt := OptionButton.new()
	opt.custom_minimum_size = Vector2(240, 0)
	opt.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	for idx in range(presets.size()):
		var raw_val := str(presets[idx])
		var disp := "disabled（禁用）" if raw_val == "disabled" else "%s · %s" % [UITokens.format_shortcut(raw_val), raw_val]
		opt.add_item(disp, idx)
		opt.set_item_metadata(idx, raw_val)
	row.add_child(opt)
	return opt


func _build_tab_mcp() -> void:
	var t5 := _make_page_scroll("MCP 服务")

	# Structured MCP Service Rows Card
	var list_card := _make_card(t5, 6, 12, 10)
	_mcp_rows_box = VBoxContainer.new()
	_mcp_rows_box.add_theme_constant_override("separation", 6)
	list_card.add_child(_mcp_rows_box)

	# Detail Editor Card
	var det_card := _make_card(t5, 7, 14, 10)
	_mcp_name_edit = LineEdit.new()
	_mcp_name_edit.placeholder_text = "英文/数字/下划线（例如 t3_code）"
	_make_labeled_row(det_card, "服务名称", _mcp_name_edit)

	_mcp_transport_opt = OptionButton.new()
	_mcp_transport_opt.custom_minimum_size = Vector2(180, 0)
	_mcp_transport_opt.clip_text = true
	_mcp_transport_opt.add_item("streamable_http（远程 HTTP MCP，推荐）", 0)
	_mcp_transport_opt.set_item_metadata(0, "streamable_http")
	_mcp_transport_opt.add_item("sse（远程 SSE MCP）", 1)
	_mcp_transport_opt.set_item_metadata(1, "sse")
	_mcp_transport_opt.add_item("stdio（本地命令行 MCP）", 2)
	_mcp_transport_opt.set_item_metadata(2, "stdio")
	_mcp_transport_opt.item_selected.connect(func(_idx: int) -> void: _update_mcp_field_visibility())
	_make_labeled_row(det_card, "传输方式", _mcp_transport_opt)

	_mcp_auth_opt = OptionButton.new()
	_mcp_auth_opt.custom_minimum_size = Vector2(180, 0)
	_mcp_auth_opt.clip_text = true
	_mcp_auth_opt.add_item("oauth（OAuth 2.1 浏览器授权，如 T3 Code）", 0)
	_mcp_auth_opt.set_item_metadata(0, "oauth")
	_mcp_auth_opt.add_item("none（无认证或自定义 Header）", 1)
	_mcp_auth_opt.set_item_metadata(1, "none")
	_mcp_auth_row = _make_labeled_row(det_card, "认证方式", _mcp_auth_opt)

	_mcp_url_edit = LineEdit.new()
	_mcp_url_edit.placeholder_text = "https://.../mcp"
	_mcp_url_row = _make_labeled_row(det_card, "远程 URL", _mcp_url_edit)

	_mcp_cmd_edit = LineEdit.new()
	_mcp_cmd_edit.placeholder_text = "例如 npx 或 uvx"
	_mcp_cmd_row = _make_labeled_row(det_card, "启动命令", _mcp_cmd_edit)

	_mcp_args_edit = LineEdit.new()
	_mcp_args_edit.placeholder_text = "空格分隔参数"
	_mcp_args_row = _make_labeled_row(det_card, "命令参数", _mcp_args_edit)

	_mcp_safe_tools_edit = LineEdit.new()
	_mcp_safe_tools_edit.placeholder_text = "可选，逗号分隔免审批只读工具名"
	_make_labeled_row(det_card, "只读白名单", _mcp_safe_tools_edit)

	var mcp_card_ops := HBoxContainer.new()
	mcp_card_ops.add_theme_constant_override("separation", 8)
	det_card.add_child(mcp_card_ops)

	var mcp_auth_btn := Button.new()
	mcp_auth_btn.text = "OAuth 授权"
	_style_secondary_btn(mcp_auth_btn, "plug")
	mcp_auth_btn.pressed.connect(_on_mcp_authorize_pressed)
	mcp_card_ops.add_child(mcp_auth_btn)

	var ops_sp := Control.new()
	ops_sp.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	mcp_card_ops.add_child(ops_sp)

	var mcp_del_btn := Button.new()
	mcp_del_btn.text = "删除服务"
	mcp_del_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
	UITokens.apply_button_states(
		mcp_del_btn,
		Color.TRANSPARENT,
		UITokens.STATUS_RED_BG,
		UITokens.STATUS_RED_HOVER,
		Color.TRANSPARENT,
		8,
		UITokens.STATUS_RED,
		10,
		5,
		UITokens.STATUS_RED
	)
	mcp_del_btn.pressed.connect(_on_mcp_delete_pressed)
	mcp_card_ops.add_child(mcp_del_btn)

	_mcp_auth_url_row = HBoxContainer.new()
	_mcp_auth_url_row.add_theme_constant_override("separation", 6)
	_mcp_auth_url_row.visible = false
	t5.add_child(_mcp_auth_url_row)
	var aul := Label.new()
	aul.text = "授权链接:"
	_mcp_auth_url_row.add_child(aul)
	_mcp_auth_url_edit = LineEdit.new()
	_mcp_auth_url_edit.editable = false
	_mcp_auth_url_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_mcp_auth_url_row.add_child(_mcp_auth_url_edit)
	var open_url_btn := UITokens.make_ghost_button("浏览器打开", "external_link", 13)
	open_url_btn.pressed.connect(func() -> void:
		if _mcp_auth_url_edit.text.strip_edges() != "":
			OS.shell_open(_mcp_auth_url_edit.text.strip_edges())
	)
	_mcp_auth_url_row.add_child(open_url_btn)

	_mcp_tools_label = _add_hint_label(t5, "提示：添加支持 OAuth 2.1 动态注册的远程 MCP 后，点右下角「保存并连接」，再点「OAuth 授权」完成登录即可。")
	_update_mcp_field_visibility()


# --- Routing & Providers UI Logic ---

func _sync_routing_and_providers_ui(data: Dictionary) -> void:
	var provs: Variant = data.get("providers", {})
	_providers_data = provs.duplicate(true) if typeof(provs) == TYPE_DICTIONARY else {}
	_provider_order.clear()
	for k in _providers_data.keys():
		_provider_order.append(str(k))

	var routing: Variant = data.get("model_routing", {})
	_routing_data = routing.duplicate(true) if typeof(routing) == TYPE_DICTIONARY else {}

	_is_syncing_ui = true
	for slot_key in _slot_widgets.keys():
		var w: Dictionary = _slot_widgets[slot_key]
		var prov_opt: OptionButton = w["prov_opt"]
		var model_edit: LineEdit = w["model_edit"]
		var voice_opt: OptionButton = w["voice_opt"]
		prov_opt.clear()

		var s_cfg: Dictionary = _routing_data.get(slot_key, {})
		var cur_pid := str(s_cfg.get("provider", ""))
		var cur_model := str(s_cfg.get("model", ""))
		var sel_idx := 0
		for i in range(_provider_order.size()):
			var pid := _provider_order[i]
			var pdict: Dictionary = _providers_data.get(pid, {})
			var plabel := str(pdict.get("label", pid))
			prov_opt.add_item(plabel, i)
			prov_opt.set_item_metadata(i, pid)
			if pid == cur_pid:
				sel_idx = i
		if not _provider_order.is_empty():
			prov_opt.select(sel_idx)
			_populate_slot_presets(slot_key, _provider_order[sel_idx])
		model_edit.text = cur_model
		if voice_opt != null:
			var cur_voice := str(s_cfg.get("voice", "stella"))
			var v_idx := VOICE_OPTIONS.find(cur_voice)
			voice_opt.select(v_idx if v_idx >= 0 else 0)
	_is_syncing_ui = false
	_rebuild_providers_pool()


func _populate_slot_presets(slot_key: String, pid: String) -> void:
	if not _slot_widgets.has(slot_key):
		return
	var w: Dictionary = _slot_widgets[slot_key]
	var preset_menu: MenuButton = w["preset_menu"]
	var popup := preset_menu.get_popup()
	popup.clear()
	var pdict: Dictionary = _providers_data.get(pid, {})
	var models: Variant = pdict.get("models", [])
	if typeof(models) == TYPE_ARRAY:
		for m in models:
			var m_str := str(m).strip_edges()
			if m_str != "":
				popup.add_item(m_str)


func _on_slot_provider_changed(slot_key: String, idx: int) -> void:
	if _is_syncing_ui or not _slot_widgets.has(slot_key):
		return
	var w: Dictionary = _slot_widgets[slot_key]
	var prov_opt: OptionButton = w["prov_opt"]
	var model_edit: LineEdit = w["model_edit"]
	if idx < 0 or idx >= prov_opt.item_count:
		return
	var pid := str(prov_opt.get_item_metadata(idx))
	_populate_slot_presets(slot_key, pid)
	var pdict: Dictionary = _providers_data.get(pid, {})
	var models: Variant = pdict.get("models", [])
	if typeof(models) == TYPE_ARRAY and not (models as Array).is_empty():
		model_edit.text = str((models as Array)[0])


func _rebuild_providers_pool() -> void:
	if _providers_list_box == null:
		return
	for ch in _providers_list_box.get_children():
		ch.queue_free()

	for pid in _provider_order:
		var pdict: Dictionary = _providers_data.get(pid, {})
		var patch: Dictionary = _pending_provider_patches.get(pid, {})
		var label_txt := str(patch.get("label", pdict.get("label", pid)))
		var base_u := str(patch.get("base_url", pdict.get("base_url", "")))
		var auth_req := bool(pdict.get("auth_required", true))
		var has_new_key := str(patch.get("api_key", "")).strip_edges() != ""
		var has_key := bool(pdict.get("has_key", false)) or has_new_key

		var row_panel := PanelContainer.new()
		row_panel.add_theme_stylebox_override(
			"panel",
			UITokens.make_button_style(UITokens.SURFACE_ELEVATED, UITokens.BORDER_HAIRLINE, 9, 10, 5)
		)
		_providers_list_box.add_child(row_panel)

		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation", 8)
		row_panel.add_child(row)

		var dot := StatusDot.new()
		dot.dot_color = UITokens.STATUS_EMERALD if (has_key or not auth_req) else UITokens.TEXT_MUTED
		row.add_child(dot)

		var name_lbl := Label.new()
		name_lbl.text = label_txt
		name_lbl.custom_minimum_size = Vector2(98, 0)
		name_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
		row.add_child(name_lbl)

		var url_short := base_u.replace("https://", "").replace("http://", "")
		var url_lbl := Label.new()
		url_lbl.text = url_short
		url_lbl.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		url_lbl.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
		url_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
		url_lbl.add_theme_color_override("font_color", UITokens.TEXT_MUTED)
		row.add_child(url_lbl)

		var badge := PanelContainer.new()
		var badge_txt := "本地免密" if not auth_req else ("已配置" if has_key else "未配置")
		var badge_bg := UITokens.STATUS_EMERALD_BG if (auth_req and has_key) else UITokens.SURFACE_CHIP
		var badge_fg := UITokens.STATUS_EMERALD if (auth_req and has_key) else UITokens.TEXT_SECONDARY
		badge.add_theme_stylebox_override("panel", UITokens.make_button_style(badge_bg, Color.TRANSPARENT, 6, 8, 2))
		var b_lbl := Label.new()
		b_lbl.text = badge_txt
		b_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
		b_lbl.add_theme_color_override("font_color", badge_fg)
		badge.add_child(b_lbl)
		row.add_child(badge)

		var captured_pid := pid
		var edit_btn := UITokens.make_ghost_button("编辑")
		edit_btn.pressed.connect(func() -> void: _open_provider_editor(captured_pid))
		row.add_child(edit_btn)

		var test_btn := Button.new()
		test_btn.text = "测试连通"
		_style_secondary_btn(test_btn)
		test_btn.pressed.connect(func() -> void: _test_provider_connectivity(captured_pid))
		row.add_child(test_btn)


func _open_provider_editor(pid: String) -> void:
	var pdict: Dictionary = _providers_data.get(pid, {})
	var patch: Dictionary = _pending_provider_patches.get(pid, {})
	var label_txt := str(patch.get("label", pdict.get("label", pid)))
	_prov_editing_is_new = false
	_prov_id_edit.text = pid
	_prov_id_edit.editable = false
	_prov_label_edit.text = label_txt
	_prov_url_edit.text = str(patch.get("base_url", pdict.get("base_url", "")))
	var pending_key := str(patch.get("api_key", ""))
	_prov_key_edit.text = pending_key
	var masked := str(pdict.get("key_masked", ""))
	_prov_key_edit.placeholder_text = ("当前密钥 %s（留空保留原值）" % masked) if masked != "" else "输入 API Key（本地服务可留空）"
	if _prov_editor_title != null:
		_prov_editor_title.text = "正在编辑：%s（输入 API Key 后点击右下角「保存并生效」）" % label_txt
	_prov_editor_card.visible = true
	_set_status("正在编辑 %s，输入 API Key 后点击右下角「保存并生效」" % label_txt, UITokens.ACCENT_CYAN)
	call_deferred("_reveal_provider_editor", false)


func _open_new_provider_editor() -> void:
	_prov_editing_is_new = true
	_prov_id_edit.text = ""
	_prov_id_edit.editable = true
	_prov_label_edit.text = ""
	_prov_url_edit.text = ""
	_prov_key_edit.text = ""
	_prov_key_edit.placeholder_text = "输入 API Key（本地服务可留空）"
	if _prov_editor_title != null:
		_prov_editor_title.text = "新增服务商（填写后点击右下角「保存并生效」）"
	_prov_editor_card.visible = true
	_set_status("正在新增服务商，填写完成后点击右下角「保存并生效」", UITokens.ACCENT_CYAN)
	call_deferred("_reveal_provider_editor", true)


func _reveal_provider_editor(focus_id: bool) -> void:
	if _routing_scroll != null and _prov_editor_card != null and _prov_editor_card.visible:
		_routing_scroll.ensure_control_visible(_prov_editor_card)
	if focus_id and _prov_id_edit != null:
		_prov_id_edit.grab_focus()
	elif _prov_key_edit != null:
		_prov_key_edit.grab_focus()


func _commit_provider_editor_patch() -> void:
	if _prov_editor_card == null or not _prov_editor_card.visible:
		return
	var pid := _prov_id_edit.text.strip_edges()
	if pid == "":
		return
	var patch: Dictionary = _pending_provider_patches.get(pid, {}).duplicate()
	var lbl := _prov_label_edit.text.strip_edges()
	var url_s := _prov_url_edit.text.strip_edges()
	var key_s := _prov_key_edit.text.strip_edges()
	if lbl != "":
		patch["label"] = lbl
	elif not patch.has("label"):
		patch["label"] = pid
	if url_s != "":
		patch["base_url"] = url_s
	if key_s != "":
		patch["api_key"] = key_s
	_pending_provider_patches[pid] = patch
	if not _providers_data.has(pid):
		_providers_data[pid] = {
			"name": pid,
			"label": str(patch.get("label", pid)),
			"base_url": url_s,
			"has_key": key_s != "",
			"auth_required": true,
			"models": [],
		}
		_provider_order.append(pid)
	_rebuild_providers_pool()


func _test_provider_connectivity(pid: String) -> void:
	if _probe_http == null:
		return
	_probing_provider_id = pid
	var pdict: Dictionary = _providers_data.get(pid, {})
	var patch: Dictionary = _pending_provider_patches.get(pid, {})
	var payload: Dictionary = {
		"provider_id": pid,
		"base_url": str(patch.get("base_url", pdict.get("base_url", ""))),
		"protocol": str(pdict.get("protocol", "openai_compatible")),
	}
	var live_key := str(patch.get("api_key", "")).strip_edges()
	if _prov_editor_card.visible and _prov_id_edit.text.strip_edges() == pid:
		if _prov_url_edit.text.strip_edges() != "":
			payload["base_url"] = _prov_url_edit.text.strip_edges()
		if _prov_key_edit.text.strip_edges() != "":
			live_key = _prov_key_edit.text.strip_edges()
	if live_key != "":
		payload["api_key"] = live_key
	var models: Variant = pdict.get("models", [])
	if typeof(models) == TYPE_ARRAY and not (models as Array).is_empty():
		payload["model"] = str((models as Array)[0])

	_set_status("正在探测 %s 连通性…" % str(pdict.get("label", pid)), UITokens.ACCENT_CYAN)
	_probe_http.cancel_request()
	_probe_http.request(
		"%s/api/config/test_provider" % http_base,
		["Content-Type: application/json"],
		HTTPClient.METHOD_POST,
		JSON.stringify(payload)
	)


func _on_probe_http_completed(_result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	var parsed: Variant = JSON.parse_string(body.get_string_from_utf8())
	if code < 200 or code >= 300 or typeof(parsed) != TYPE_DICTIONARY:
		_set_status("连通性探测请求失败 (HTTP %d)" % code, UITokens.STATUS_RED)
		return
	var d: Dictionary = parsed
	if bool(d.get("ok", false)):
		_set_status("%s · %s" % [_probing_provider_id, str(d.get("message", "连通成功"))], UITokens.STATUS_EMERALD)
	else:
		_set_status("%s · %s" % [_probing_provider_id, str(d.get("message", "连通失败"))], UITokens.STATUS_RED)


# --- Shortcuts Helpers ---

func _select_or_add_hotkey(opt: OptionButton, raw_val: String) -> void:
	if opt == null:
		return
	var target := raw_val.strip_edges().to_lower()
	if target == "":
		target = "disabled"
	for idx in range(opt.item_count):
		var meta: Variant = opt.get_item_metadata(idx)
		if str(meta).to_lower() == target:
			opt.select(idx)
			return
	var new_idx := opt.item_count
	opt.add_item("%s · %s" % [UITokens.format_shortcut(target), target], new_idx)
	opt.set_item_metadata(new_idx, target)
	opt.select(new_idx)


func _get_selected_hotkey(opt: OptionButton) -> String:
	if opt == null or opt.selected < 0 or opt.selected >= opt.item_count:
		return "disabled"
	var meta: Variant = opt.get_item_metadata(opt.selected)
	return str(meta) if meta != null else opt.get_item_text(opt.selected)


func _reset_shortcut_defaults() -> void:
	_select_or_add_hotkey(_sc_ptt_opt, "right_option")
	_select_or_add_hotkey(_sc_chat_opt, "option+space")
	_select_or_add_hotkey(_sc_cancel_opt, "option+esc")
	_select_or_add_hotkey(_sc_dock_opt, "option+h")
	_select_or_add_hotkey(_sc_settings_opt, "option+k")
	_set_status("已恢复默认快捷键，点击右下角「保存并生效」应用", UITokens.ACCENT_CYAN)


# --- Settings Load & Save ---

func _set_status(msg: String, color: Color = UITokens.TEXT_SECONDARY) -> void:
	if _status_label == null:
		return
	_status_label.text = msg
	_status_label.add_theme_color_override("font_color", color)


func _fetch_backend_settings() -> void:
	_pending_mode = "get"
	_set_status("正在读取配置…", UITokens.TEXT_SECONDARY)
	var err := _http.request("%s/api/config/settings" % http_base, [], HTTPClient.METHOD_GET)
	if err != OK:
		_set_status("读取配置失败 (%d)" % err, UITokens.STATUS_RED)


func _on_http_completed(_result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	var txt := body.get_string_from_utf8()
	var parsed: Variant = JSON.parse_string(txt)
	if code < 200 or code >= 300 or typeof(parsed) != TYPE_DICTIONARY:
		_set_status("请求异常 HTTP %d" % code, UITokens.STATUS_RED)
		_primary_btn.disabled = false
		return

	var dict: Dictionary = parsed
	if _pending_mode == "get":
		_apply_loaded_settings(dict)
		_set_status("本地 0600 安全存储（密钥已脱敏）", UITokens.TEXT_SECONDARY)
	elif _pending_mode == "post":
		_primary_btn.disabled = false
		_pending_provider_patches.clear()
		if _prov_editor_card != null:
			_prov_editor_card.visible = false
		var s_dict: Dictionary = dict.get("settings", {})
		if not s_dict.is_empty():
			_apply_loaded_settings(s_dict)
		_set_status("保存成功，已写入 config.yaml (0600) 并热更新生效", UITokens.STATUS_EMERALD)


func _apply_loaded_settings(data: Dictionary) -> void:
	_loaded_settings = data
	_sync_routing_and_providers_ui(data)

	var audio_sec: Dictionary = data.get("audio", {})
	_audio_enabled_sw.call("set_checked", bool(audio_sec.get("enabled", true)), false)

	var ptt_sec: Dictionary = audio_sec.get("ptt", {})
	var sc_sec: Dictionary = data.get("shortcuts", {})
	var ptt_hk := str(sc_sec.get("ptt_hotkey", ptt_sec.get("hotkey", "right_option")))
	_select_or_add_hotkey(_sc_ptt_opt, ptt_hk)
	_select_or_add_hotkey(_sc_chat_opt, str(sc_sec.get("chat_input_hotkey", "option+space")))
	_select_or_add_hotkey(_sc_cancel_opt, str(sc_sec.get("cancel_hotkey", "option+esc")))
	_select_or_add_hotkey(_sc_dock_opt, str(sc_sec.get("toggle_dock_hotkey", "option+h")))
	_select_or_add_hotkey(_sc_settings_opt, str(sc_sec.get("settings_hotkey", "option+k")))

	var priv_sec: Dictionary = data.get("privacy", {})
	var pmode := str(priv_sec.get("default_mode", "meta_only"))
	var pm_idx := PRIVACY_OPTIONS.find(pmode)
	_privacy_mode_opt.select(pm_idx if pm_idx >= 0 else 1)

	var batch_sec: Dictionary = data.get("batch_context", {})
	_inbox_dir_edit.text = str(batch_sec.get("inbox_dir", "~/Desktop/给启明星"))

	var godot_sec: Dictionary = data.get("godot", {})
	_auto_hide_sw.call("set_checked", bool(godot_sec.get("auto_hide_on_capture", true)), false)


func _on_primary_action_pressed() -> void:
	if _tabs != null and _tabs.current_tab == 4:
		_on_mcp_save_pressed()
	else:
		_on_save_pressed()


func _on_save_pressed() -> void:
	_commit_provider_editor_patch()

	var ptt_hk := _get_selected_hotkey(_sc_ptt_opt)
	var chat_hk := _get_selected_hotkey(_sc_chat_opt)
	var cancel_hk := _get_selected_hotkey(_sc_cancel_opt)
	var dock_hk := _get_selected_hotkey(_sc_dock_opt)
	var settings_hk := _get_selected_hotkey(_sc_settings_opt)

	var seen_hotkeys: Dictionary = {}
	for hk in [ptt_hk, chat_hk, cancel_hk, dock_hk, settings_hk]:
		if hk == "" or hk == "disabled":
			continue
		if seen_hotkeys.has(hk):
			_set_status("快捷键冲突: 「%s」重复分配，请修改后再保存" % hk, UITokens.STATUS_RED)
			return
		seen_hotkeys[hk] = true

	var new_scale := _scale_slider.value / 100.0
	var roam_on := bool(_roam_sw.call("is_checked"))
	var earcons_on := bool(_earcons_sw.call("is_checked"))
	var ambient_on := bool(_ambient_sw.call("is_checked"))
	var auto_hide := bool(_auto_hide_sw.call("is_checked"))
	prefs_applied.emit(new_scale, roam_on, earcons_on, ambient_on, auto_hide)

	var routing_payload: Dictionary = {}
	for slot_key in _slot_widgets.keys():
		var w: Dictionary = _slot_widgets[slot_key]
		var prov_opt: OptionButton = w["prov_opt"]
		var model_edit: LineEdit = w["model_edit"]
		var voice_opt: OptionButton = w["voice_opt"]
		var pid := str(prov_opt.get_item_metadata(prov_opt.selected)) if prov_opt.selected >= 0 else "deepseek"
		var slot_d := {
			"provider": pid,
			"model": model_edit.text.strip_edges(),
		}
		if voice_opt != null:
			slot_d["voice"] = VOICE_OPTIONS[clampi(voice_opt.selected, 0, VOICE_OPTIONS.size() - 1)]
		routing_payload[slot_key] = slot_d

	var payload: Dictionary = {
		"audio": {
			"enabled": bool(_audio_enabled_sw.call("is_checked")),
			"ptt": {"hotkey": ptt_hk},
		},
		"shortcuts": {
			"ptt_hotkey": ptt_hk,
			"chat_input_hotkey": chat_hk,
			"cancel_hotkey": cancel_hk,
			"toggle_dock_hotkey": dock_hk,
			"settings_hotkey": settings_hk,
		},
		"privacy": {
			"default_mode": PRIVACY_OPTIONS[clampi(_privacy_mode_opt.selected, 0, PRIVACY_OPTIONS.size() - 1)],
		},
		"batch_context": {
			"inbox_dir": _inbox_dir_edit.text.strip_edges(),
		},
		"godot": {
			"auto_hide_on_capture": auto_hide,
			"earcons_enabled": earcons_on,
		},
		"model_routing": routing_payload,
	}
	if not _pending_provider_patches.is_empty():
		payload["providers"] = _pending_provider_patches

	_pending_mode = "post"
	_primary_btn.disabled = true
	_set_status("正在保存并热更新路由配置…", UITokens.ACCENT_CYAN)
	var err := _http.request(
		"%s/api/config/settings" % http_base,
		["Content-Type: application/json"],
		HTTPClient.METHOD_POST,
		JSON.stringify(payload)
	)
	if err != OK:
		_primary_btn.disabled = false
		_set_status("保存请求失败 (%d)" % err, UITokens.STATUS_RED)


# --- Tab 4: Structured MCP Services ---

func _update_mcp_field_visibility() -> void:
	if _mcp_transport_opt == null:
		return
	var tr := str(_mcp_transport_opt.get_item_metadata(_mcp_transport_opt.selected))
	var is_remote := tr == "streamable_http" or tr == "sse"
	_mcp_auth_row.visible = is_remote
	_mcp_url_row.visible = is_remote
	_mcp_cmd_row.visible = not is_remote
	_mcp_args_row.visible = not is_remote


func _clear_mcp_editor() -> void:
	_mcp_selected_name = ""
	_mcp_name_edit.text = ""
	_mcp_transport_opt.select(0)
	_mcp_auth_opt.select(0)
	_mcp_url_edit.text = ""
	_mcp_cmd_edit.text = ""
	_mcp_args_edit.text = ""
	_mcp_safe_tools_edit.text = ""
	_mcp_enabled_val = true
	_mcp_auth_url_row.visible = false
	_mcp_auth_url_edit.text = ""
	_update_mcp_field_visibility()
	_rebuild_mcp_rows()
	_set_status("已准备好新建 MCP 服务，填写后点击「保存并连接」", UITokens.TEXT_SECONDARY)


func _fetch_mcp_servers() -> void:
	if _mcp_http == null:
		return
	_mcp_pending_op = "list"
	_mcp_http.cancel_request()
	_mcp_http.request("%s/api/mcp/servers" % http_base, [], HTTPClient.METHOD_GET)


func _select_mcp_item(idx: int) -> void:
	if idx < 0 or idx >= _mcp_servers.size():
		return
	var d_var: Variant = _mcp_servers[idx]
	if typeof(d_var) != TYPE_DICTIONARY:
		return
	var d: Dictionary = d_var
	_mcp_selected_name = str(d.get("name", ""))
	_mcp_name_edit.text = _mcp_selected_name
	var tr := str(d.get("transport", "streamable_http"))
	for i in range(_mcp_transport_opt.item_count):
		if str(_mcp_transport_opt.get_item_metadata(i)) == tr:
			_mcp_transport_opt.select(i)
			break
	var am := str(d.get("auth_mode", "none"))
	for i in range(_mcp_auth_opt.item_count):
		if str(_mcp_auth_opt.get_item_metadata(i)) == am:
			_mcp_auth_opt.select(i)
			break
	_mcp_url_edit.text = str(d.get("url", ""))
	_mcp_cmd_edit.text = str(d.get("command", ""))
	var args_arr: Variant = d.get("args", [])
	_mcp_args_edit.text = " ".join(PackedStringArray(args_arr)) if typeof(args_arr) == TYPE_ARRAY else ""
	var safe_arr: Variant = d.get("safe_tools", [])
	_mcp_safe_tools_edit.text = ", ".join(PackedStringArray(safe_arr)) if typeof(safe_arr) == TYPE_ARRAY else ""
	_mcp_enabled_val = bool(d.get("enabled", true))
	_update_mcp_field_visibility()
	_render_mcp_detail(d)
	_rebuild_mcp_rows()


func _render_mcp_detail(d: Dictionary) -> void:
	var st := str(d.get("status", "offline"))
	var err := str(d.get("error", ""))
	var tools: Variant = d.get("tools", [])
	var tools_arr: Array = tools if typeof(tools) == TYPE_ARRAY else []
	var summary := "状态: %s" % st
	if not tools_arr.is_empty():
		var t_names: PackedStringArray = []
		for t in tools_arr:
			t_names.append(str(t))
		summary += " · 已挂载 %d 个工具: %s" % [tools_arr.size(), ", ".join(t_names)]
	elif err != "":
		summary += " · %s" % err
	_mcp_tools_label.text = summary


func _rebuild_mcp_rows() -> void:
	if _mcp_rows_box == null:
		return
	for ch in _mcp_rows_box.get_children():
		ch.queue_free()

	if _mcp_servers.is_empty():
		var empty_lbl := Label.new()
		empty_lbl.text = "暂未配置 MCP 服务，点击右上角「+ 添加服务」快速接入"
		empty_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
		empty_lbl.add_theme_color_override("font_color", UITokens.TEXT_MUTED)
		_mcp_rows_box.add_child(empty_lbl)
		return

	for i in range(_mcp_servers.size()):
		var item_var: Variant = _mcp_servers[i]
		if typeof(item_var) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = item_var
		var sname := str(d.get("name", ""))
		var tr := str(d.get("transport", "stdio"))
		var st := str(d.get("status", "offline"))
		var enabled_b := bool(d.get("enabled", true))
		var tools: Variant = d.get("tools", [])
		var tcount := (tools as Array).size() if typeof(tools) == TYPE_ARRAY else 0
		var is_sel := (sname == _mcp_selected_name)

		var row_panel := PanelContainer.new()
		row_panel.add_theme_stylebox_override(
			"panel",
			UITokens.make_button_style(
				UITokens.SURFACE_HOVER if is_sel else UITokens.SURFACE_ELEVATED,
				UITokens.ACCENT_CYAN_BORDER if is_sel else UITokens.BORDER_HAIRLINE,
				9,
				10,
				6
			)
		)
		_mcp_rows_box.add_child(row_panel)

		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation", 8)
		row_panel.add_child(row)

		var dot := StatusDot.new()
		if st == "connected":
			dot.dot_color = UITokens.STATUS_EMERALD
		elif st == "needs_auth":
			dot.dot_color = UITokens.STATUS_AMBER
		elif st == "offline" and enabled_b:
			dot.dot_color = UITokens.STATUS_RED
		else:
			dot.dot_color = UITokens.TEXT_MUTED
		row.add_child(dot)

		var sel_btn := Button.new()
		sel_btn.text = sname
		sel_btn.clip_text = true
		sel_btn.alignment = HORIZONTAL_ALIGNMENT_LEFT
		sel_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		sel_btn.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_BODY)
		UITokens.apply_button_states(
			sel_btn,
			Color.TRANSPARENT,
			Color.TRANSPARENT,
			Color.TRANSPARENT,
			Color.TRANSPARENT,
			6,
			UITokens.TEXT_PRIMARY,
			2,
			2
		)
		var captured_i := i
		sel_btn.pressed.connect(func() -> void: _select_mcp_item(captured_i))
		row.add_child(sel_btn)

		var proto_txt := "HTTP" if tr == "streamable_http" else ("SSE" if tr == "sse" else "stdio")
		var badge := PanelContainer.new()
		badge.add_theme_stylebox_override(
			"panel",
			UITokens.make_button_style(UITokens.SURFACE_CHIP, UITokens.BORDER_HAIRLINE, 6, 7, 2)
		)
		var b_lbl := Label.new()
		b_lbl.text = proto_txt
		b_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_KEYCAP)
		b_lbl.add_theme_color_override("font_color", UITokens.TEXT_SECONDARY)
		badge.add_child(b_lbl)
		row.add_child(badge)

		var tc_lbl := Label.new()
		tc_lbl.text = "%d 工具" % tcount
		tc_lbl.custom_minimum_size = Vector2(54, 0)
		tc_lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
		tc_lbl.add_theme_font_size_override("font_size", UITokens.FONT_SIZE_SMALL)
		tc_lbl.add_theme_color_override("font_color", UITokens.TEXT_MUTED)
		row.add_child(tc_lbl)

		var sw: Control = ToggleSwitch.new()
		sw.checked = enabled_b
		sw.toggled.connect(func(is_on: bool) -> void:
			_select_mcp_item(captured_i)
			_mcp_enabled_val = is_on
			_on_mcp_save_pressed()
		)
		row.add_child(sw)


func _apply_mcp_servers_list(servers: Array) -> void:
	_mcp_servers = servers
	var match_idx := -1
	for i in range(servers.size()):
		var item_var: Variant = servers[i]
		if typeof(item_var) == TYPE_DICTIONARY and str((item_var as Dictionary).get("name", "")) == _mcp_selected_name:
			match_idx = i
			break
	if match_idx >= 0:
		_select_mcp_item(match_idx)
	elif not servers.is_empty() and _mcp_name_edit.text.strip_edges() == "":
		_select_mcp_item(0)
	else:
		_rebuild_mcp_rows()


func _build_mcp_payload() -> Dictionary:
	var tr := str(_mcp_transport_opt.get_item_metadata(_mcp_transport_opt.selected))
	var am := str(_mcp_auth_opt.get_item_metadata(_mcp_auth_opt.selected))
	return {
		"name": _mcp_name_edit.text.strip_edges(),
		"enabled": _mcp_enabled_val,
		"transport": tr,
		"auth_mode": am if (tr == "streamable_http" or tr == "sse") else "none",
		"url": _mcp_url_edit.text.strip_edges(),
		"command": _mcp_cmd_edit.text.strip_edges(),
		"args": _mcp_args_edit.text.strip_edges(),
		"safe_tools": _mcp_safe_tools_edit.text.strip_edges(),
	}


func _on_mcp_save_pressed() -> void:
	var payload := _build_mcp_payload()
	if str(payload.get("name", "")) == "":
		_set_status("请填写 MCP 服务名称（如 t3_code）", UITokens.STATUS_RED)
		return
	_mcp_selected_name = str(payload["name"])
	_mcp_pending_op = "save"
	_set_status("正在保存并连接 MCP 服务「%s」…" % _mcp_selected_name, UITokens.ACCENT_CYAN)
	_mcp_http.cancel_request()
	_mcp_http.request(
		"%s/api/mcp/servers" % http_base,
		["Content-Type: application/json"],
		HTTPClient.METHOD_POST,
		JSON.stringify(payload),
	)


func _on_mcp_authorize_pressed() -> void:
	var sname := _mcp_name_edit.text.strip_edges()
	if sname == "":
		_set_status("请先填写并保存需要 OAuth 授权的 MCP 名称", UITokens.STATUS_RED)
		return
	_mcp_pending_op = "authorize"
	_mcp_auth_url_row.visible = false
	_set_status("正在向「%s」发起 OAuth 2.1 动态注册与授权…" % sname, UITokens.ACCENT_CYAN)
	_mcp_http.cancel_request()
	_mcp_http.request(
		"%s/api/mcp/servers/%s/authorize" % [http_base, sname.uri_encode()],
		["Content-Type: application/json"],
		HTTPClient.METHOD_POST,
		"{}",
	)


func _on_mcp_delete_pressed() -> void:
	var sname := _mcp_name_edit.text.strip_edges()
	if sname == "":
		return
	_mcp_pending_op = "delete"
	_set_status("正在删除 MCP「%s」…" % sname, UITokens.TEXT_SECONDARY)
	_mcp_http.cancel_request()
	_mcp_http.request(
		"%s/api/mcp/servers/%s" % [http_base, sname.uri_encode()],
		[],
		HTTPClient.METHOD_DELETE,
	)


func _on_mcp_poll_timeout() -> void:
	_mcp_poll_ticks += 1
	if _mcp_poll_ticks > 25:
		_mcp_poll_timer.stop()
		return
	_fetch_mcp_servers()


func _on_mcp_http_completed(_result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	var txt := body.get_string_from_utf8()
	var parsed: Variant = JSON.parse_string(txt)
	if code < 200 or code >= 300 or typeof(parsed) != TYPE_DICTIONARY:
		var detail := ""
		if typeof(parsed) == TYPE_DICTIONARY:
			detail = str((parsed as Dictionary).get("detail", ""))
		_set_status("MCP 请求失败 (%d)%s" % [code, (": " + detail) if detail != "" else ""], UITokens.STATUS_RED)
		return

	var dict: Dictionary = parsed
	var srv_arr: Variant = dict.get("servers", [])
	if typeof(srv_arr) == TYPE_ARRAY:
		_apply_mcp_servers_list(srv_arr)

	if _mcp_pending_op == "save":
		_set_status("已保存 MCP 配置并尝试连接；若需登录请点击「OAuth 授权」", UITokens.STATUS_EMERALD)
	elif _mcp_pending_op == "delete":
		_clear_mcp_editor()
		_set_status("已删除该 MCP 配置并清理授权缓存", UITokens.TEXT_SECONDARY)
	elif _mcp_pending_op == "authorize":
		var st := str(dict.get("status", ""))
		var auth_url := str(dict.get("auth_url", ""))
		if st == "authorizing" and auth_url != "":
			_mcp_auth_url_edit.text = auth_url
			_mcp_auth_url_row.visible = true
			_mcp_tools_label.text = "请在浏览器中批准登录（授权完成后将自动连接并同步工具）"
			_set_status("已打开浏览器 OAuth 授权页，等待回调中…", UITokens.ACCENT_CYAN)
			_mcp_poll_ticks = 0
			_mcp_poll_timer.start()
		elif st == "connected":
			_mcp_poll_timer.stop()
			_mcp_auth_url_row.visible = false
			_set_status("MCP 授权并连接成功！", UITokens.STATUS_EMERALD)
		else:
			_mcp_poll_timer.stop()
			_set_status("OAuth 授权失败: %s" % str(dict.get("error", "未知错误")), UITokens.STATUS_RED)
	elif _mcp_pending_op == "list":
		var cur_name := _mcp_name_edit.text.strip_edges()
		for item_var in _mcp_servers:
			if typeof(item_var) == TYPE_DICTIONARY and str((item_var as Dictionary).get("name", "")) == cur_name:
				if str((item_var as Dictionary).get("status", "")) == "connected":
					_mcp_poll_timer.stop()
					_mcp_auth_url_row.visible = false
					_set_status("MCP「%s」已授权连接成功，工具已挂载！" % cur_name, UITokens.STATUS_EMERALD)
				break


class StarEmblem extends Control:
	func _init() -> void:
		custom_minimum_size = Vector2(18, 22)
		size_flags_vertical = Control.SIZE_SHRINK_CENTER
		mouse_filter = Control.MOUSE_FILTER_IGNORE

	func _draw() -> void:
		var c := Vector2(9.0, 11.0)
		var outer_r := 7.2
		var inner_r := 2.2
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
		draw_colored_polygon(pts, UITokens.ACCENT_CYAN_HOVER)


class StatusDot extends Control:
	var dot_color: Color = Color(0.2039, 0.8275, 0.6000, 1.0)

	func _init() -> void:
		custom_minimum_size = Vector2(8, 16)
		size_flags_vertical = Control.SIZE_SHRINK_CENTER
		mouse_filter = Control.MOUSE_FILTER_IGNORE

	func _draw() -> void:
		draw_circle(Vector2(4.0, size.y * 0.5), 3.2, dot_color)
