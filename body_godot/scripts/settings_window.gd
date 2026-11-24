extends Window

signal prefs_applied(avatar_scale: float, roam_on: bool, earcons_on: bool, ambient_on: bool, auto_hide: bool)
signal scale_previewed(avatar_scale: float)
signal reset_position_requested
signal reload_mcp_requested
signal open_web_requested

var http_base: String = "http://127.0.0.1:18790"

var _http: HTTPRequest
var _pending_mode: String = "" # "get" | "post"
var _loaded_settings: Dictionary = {}
var _profile_names: Array[String] = []
var _is_syncing_ui: bool = false
var _tabs: TabContainer

# UI widgets — Tab 1: 外观与交互
var _scale_slider: HSlider
var _scale_val_label: Label
var _roam_check: CheckButton
var _earcons_check: CheckButton
var _ambient_check: CheckButton
var _auto_hide_check: CheckButton

# UI widgets — Tab 2: 模型与 API Key
var _default_profile_opt: OptionButton
var _ds_key_edit: LineEdit
var _ds_key_status: Label
var _prof_select_opt: OptionButton
var _prof_model_edit: LineEdit
var _prof_url_edit: LineEdit
var _prof_key_edit: LineEdit
var _prof_key_status: Label

# UI widgets — Tab 3: 语音与隐私
var _audio_enabled_check: CheckButton
var _tts_voice_opt: OptionButton
var _privacy_mode_opt: OptionButton
var _inbox_dir_edit: LineEdit

# UI widgets — Tab 4: 快捷键
var _sc_ptt_opt: OptionButton
var _sc_chat_opt: OptionButton
var _sc_cancel_opt: OptionButton
var _sc_dock_opt: OptionButton
var _sc_settings_opt: OptionButton

# UI widgets — Tab 5: MCP 服务
var _mcp_http: HTTPRequest
var _mcp_pending_op: String = ""
var _mcp_servers: Array = []
var _mcp_list: ItemList
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
var _mcp_enabled_check: CheckButton
var _mcp_auth_url_row: HBoxContainer
var _mcp_auth_url_edit: LineEdit
var _mcp_tools_label: Label
var _mcp_poll_timer: Timer
var _mcp_poll_ticks: int = 0

# Footer
var _status_label: Label
var _save_btn: Button

const VOICE_OPTIONS := ["stella", "tide"]
const PRIVACY_OPTIONS := ["off", "meta_only", "full_on_demand"]
const PTT_HOTKEY_PRESETS := ["right_option", "left_option", "option+space", "ctrl+space", "fn", "disabled"]
const CHAT_HOTKEY_PRESETS := ["option+space", "ctrl+space", "option+t", "option+enter", "f2", "disabled"]
const CANCEL_HOTKEY_PRESETS := ["option+esc", "ctrl+esc", "option+x", "f4", "disabled"]
const DOCK_HOTKEY_PRESETS := ["option+h", "option+d", "ctrl+option+h", "f3", "disabled"]
const SETTINGS_HOTKEY_PRESETS := ["option+k", "option+s", "ctrl+option+s", "disabled"]


func _ready() -> void:
	_http = HTTPRequest.new()
	add_child(_http)
	_http.request_completed.connect(_on_http_completed)
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
	_build_ui()


func open_settings(
	cur_http_base: String,
	cur_scale: float,
	cur_roam: bool,
	cur_earcons: bool,
	cur_ambient: bool,
	initial_tab: int = 0
) -> void:
	http_base = cur_http_base.trim_suffix("/")
	_is_syncing_ui = true
	_scale_slider.value = cur_scale * 100.0
	_scale_val_label.text = "%d%%" % int(round(cur_scale * 100.0))
	_roam_check.button_pressed = cur_roam
	_earcons_check.button_pressed = cur_earcons
	_ambient_check.button_pressed = cur_ambient
	_ds_key_edit.text = ""
	_prof_key_edit.text = ""
	if _tabs != null and _tabs.get_tab_count() > 0:
		_tabs.current_tab = clampi(initial_tab, 0, _tabs.get_tab_count() - 1)
	_is_syncing_ui = false

	visible = true
	grab_focus()
	_fetch_backend_settings()
	_fetch_mcp_servers()


func _make_bg_panel() -> PanelContainer:
	var pc := PanelContainer.new()
	pc.set_anchors_preset(Control.PRESET_FULL_RECT)
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.08, 0.11, 0.16, 1.0)
	sb.content_margin_left = 14
	sb.content_margin_right = 14
	sb.content_margin_top = 12
	sb.content_margin_bottom = 12
	pc.add_theme_stylebox_override("panel", sb)
	return pc


func _add_section_label(parent: Control, txt: String) -> Label:
	var lbl := Label.new()
	lbl.text = txt
	lbl.add_theme_font_size_override("font_size", 13)
	lbl.add_theme_color_override("font_color", Color(0.65, 0.88, 1.0))
	parent.add_child(lbl)
	return lbl


func _add_hint_label(parent: Control, txt: String) -> Label:
	var lbl := Label.new()
	lbl.text = txt
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.add_theme_font_size_override("font_size", 11)
	lbl.add_theme_color_override("font_color", Color(0.66, 0.72, 0.80))
	parent.add_child(lbl)
	return lbl


func _build_ui() -> void:
	var bg := _make_bg_panel()
	add_child(bg)

	var root_col := VBoxContainer.new()
	root_col.add_theme_constant_override("separation", 10)
	bg.add_child(root_col)

	_tabs = TabContainer.new()
	_tabs.size_flags_vertical = Control.SIZE_EXPAND_FILL
	root_col.add_child(_tabs)
	var tabs := _tabs

	# --- Tab 1: 外观与交互 ---
	var tab1_scroll := ScrollContainer.new()
	tab1_scroll.name = "外观与交互"
	tabs.add_child(tab1_scroll)

	var t1 := VBoxContainer.new()
	t1.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t1.add_theme_constant_override("separation", 10)
	tab1_scroll.add_child(t1)

	_add_section_label(t1, "桌面真身大小与动态行为")
	var scale_row := HBoxContainer.new()
	scale_row.add_theme_constant_override("separation", 8)
	t1.add_child(scale_row)
	var scale_lbl := Label.new()
	scale_lbl.text = "角色缩放:"
	scale_row.add_child(scale_lbl)
	_scale_slider = HSlider.new()
	_scale_slider.min_value = 50.0
	_scale_slider.max_value = 130.0
	_scale_slider.step = 2.0
	_scale_slider.value = 78.0
	_scale_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_scale_slider.value_changed.connect(func(v: float) -> void:
		_scale_val_label.text = "%d%%" % int(round(v))
		if not _is_syncing_ui:
			scale_previewed.emit(v / 100.0)
	)
	scale_row.add_child(_scale_slider)
	_scale_val_label = Label.new()
	_scale_val_label.custom_minimum_size = Vector2(46, 0)
	_scale_val_label.text = "78%"
	scale_row.add_child(_scale_val_label)

	_roam_check = CheckButton.new()
	_roam_check.text = "允许闲置时在桌面自由漫步"
	_roam_check.button_pressed = true
	t1.add_child(_roam_check)

	_earcons_check = CheckButton.new()
	_earcons_check.text = "启用工具与提醒微音效 (Earcons)"
	_earcons_check.button_pressed = true
	t1.add_child(_earcons_check)

	_ambient_check = CheckButton.new()
	_ambient_check.text = "启用柔和环境氛围音（播报自动压低）"
	t1.add_child(_ambient_check)

	_auto_hide_check = CheckButton.new()
	_auto_hide_check.text = "截屏看屏前自动短暂隐身防自拍"
	_auto_hide_check.button_pressed = true
	t1.add_child(_auto_hide_check)

	_add_hint_label(t1, "提示：将启明星拖到屏幕左/右/上边缘松手即可像加速器一样贴边隐藏，鼠标靠近边缘自动滑出。")

	var pos_btn := Button.new()
	pos_btn.text = "重置位置 / 取消贴边"
	pos_btn.pressed.connect(func() -> void: reset_position_requested.emit())
	t1.add_child(pos_btn)

	# --- Tab 2: 模型与 API Key ---
	var tab2_scroll := ScrollContainer.new()
	tab2_scroll.name = "模型与 API Key"
	tabs.add_child(tab2_scroll)

	var t2 := VBoxContainer.new()
	t2.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t2.add_theme_constant_override("separation", 8)
	tab2_scroll.add_child(t2)

	_add_section_label(t2, "默认对话 Profile")
	_default_profile_opt = OptionButton.new()
	t2.add_child(_default_profile_opt)

	_add_section_label(t2, "DeepSeek 统一 API Key（一键同步至对话 / OCR / 联网搜索 / 语音）")
	var ds_row := HBoxContainer.new()
	ds_row.add_theme_constant_override("separation", 6)
	t2.add_child(ds_row)
	_ds_key_edit = LineEdit.new()
	_ds_key_edit.secret = true
	_ds_key_edit.placeholder_text = "留空则保留当前已保存的密钥"
	_ds_key_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	ds_row.add_child(_ds_key_edit)
	var ds_eye := Button.new()
	ds_eye.text = "显示"
	ds_eye.pressed.connect(func() -> void:
		_ds_key_edit.secret = not _ds_key_edit.secret
		ds_eye.text = "隐藏" if not _ds_key_edit.secret else "显示"
	)
	ds_row.add_child(ds_eye)
	_ds_key_status = _add_hint_label(t2, "当前状态: 加载中…")

	var sep := HSeparator.new()
	t2.add_child(sep)

	_add_section_label(t2, "单个 Profile 高级覆盖（可选）")
	_prof_select_opt = OptionButton.new()
	_prof_select_opt.item_selected.connect(_on_profile_selected)
	t2.add_child(_prof_select_opt)

	_prof_model_edit = LineEdit.new()
	_prof_model_edit.placeholder_text = "Model ID（例如 deepseek-chat / gpt-4o）"
	t2.add_child(_prof_model_edit)

	_prof_url_edit = LineEdit.new()
	_prof_url_edit.placeholder_text = "Base URL（例如 https://api.deepseek.com/v1）"
	t2.add_child(_prof_url_edit)

	var pkey_row := HBoxContainer.new()
	pkey_row.add_theme_constant_override("separation", 6)
	t2.add_child(pkey_row)
	_prof_key_edit = LineEdit.new()
	_prof_key_edit.secret = true
	_prof_key_edit.placeholder_text = "独立 API Key 覆盖（留空保留原值）"
	_prof_key_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	pkey_row.add_child(_prof_key_edit)
	var p_eye := Button.new()
	p_eye.text = "显示"
	p_eye.pressed.connect(func() -> void:
		_prof_key_edit.secret = not _prof_key_edit.secret
		p_eye.text = "隐藏" if not _prof_key_edit.secret else "显示"
	)
	pkey_row.add_child(p_eye)
	_prof_key_status = _add_hint_label(t2, "")

	# --- Tab 3: 语音与隐私 ---
	var tab3_scroll := ScrollContainer.new()
	tab3_scroll.name = "语音与隐私"
	tabs.add_child(tab3_scroll)

	var t3 := VBoxContainer.new()
	t3.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t3.add_theme_constant_override("separation", 8)
	tab3_scroll.add_child(t3)

	_add_section_label(t3, "语音交互 (DeepSeek ASR & TTS)")
	_audio_enabled_check = CheckButton.new()
	_audio_enabled_check.text = "启用流式语音识别与语音播报"
	_audio_enabled_check.button_pressed = true
	t3.add_child(_audio_enabled_check)

	var voice_row := HBoxContainer.new()
	t3.add_child(voice_row)
	var vl := Label.new()
	vl.text = "TTS 播报音色: "
	voice_row.add_child(vl)
	_tts_voice_opt = OptionButton.new()
	_tts_voice_opt.add_item("stella（自然女声）", 0)
	_tts_voice_opt.add_item("tide（沉稳男声）", 1)
	_tts_voice_opt.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	voice_row.add_child(_tts_voice_opt)

	_add_hint_label(t3, "提示：按住说话及其他全局快捷键已整合至「快捷键」标签页统一配置。")

	_add_section_label(t3, "隐私模式与收件箱路径")
	_privacy_mode_opt = OptionButton.new()
	_privacy_mode_opt.add_item("off（完全关闭桌面感知）", 0)
	_privacy_mode_opt.add_item("meta_only（仅窗口标题，推荐）", 1)
	_privacy_mode_opt.add_item("full_on_demand（按需截屏看屏）", 2)
	t3.add_child(_privacy_mode_opt)

	var inbox_row := HBoxContainer.new()
	t3.add_child(inbox_row)
	var il := Label.new()
	il.text = "收件箱目录: "
	inbox_row.add_child(il)
	_inbox_dir_edit = LineEdit.new()
	_inbox_dir_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	inbox_row.add_child(_inbox_dir_edit)

	var ops_row := HBoxContainer.new()
	ops_row.add_theme_constant_override("separation", 8)
	t3.add_child(ops_row)
	var mcp_btn := Button.new()
	mcp_btn.text = "热重载 MCP 工具"
	mcp_btn.pressed.connect(func() -> void: reload_mcp_requested.emit())
	ops_row.add_child(mcp_btn)
	var web_btn := Button.new()
	web_btn.text = "打开 Web 调试台"
	web_btn.pressed.connect(func() -> void: open_web_requested.emit())
	ops_row.add_child(web_btn)

	# --- Tab 4: 快捷键 ---
	var tab4_scroll := ScrollContainer.new()
	tab4_scroll.name = "快捷键"
	tabs.add_child(tab4_scroll)

	var t4 := VBoxContainer.new()
	t4.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t4.add_theme_constant_override("separation", 8)
	tab4_scroll.add_child(t4)

	_add_section_label(t4, "全局快捷键触发（点击「保存并生效」后实时热更新）")
	_sc_ptt_opt = _add_hotkey_row(t4, "🎙️ 麦克风按住说话 (PTT):", PTT_HOTKEY_PRESETS)
	_sc_chat_opt = _add_hotkey_row(t4, "💬 文字输入框呼出/收起:", CHAT_HOTKEY_PRESETS)
	_sc_cancel_opt = _add_hotkey_row(t4, "■ 急停播报与任务:", CANCEL_HOTKEY_PRESETS)
	_sc_dock_opt = _add_hotkey_row(t4, "🫥 贴边隐藏 / 呼出助手:", DOCK_HOTKEY_PRESETS)
	_sc_settings_opt = _add_hotkey_row(t4, "⌨️ 打开快捷键与设置:", SETTINGS_HOTKEY_PRESETS)

	var sc_ops := HBoxContainer.new()
	sc_ops.add_theme_constant_override("separation", 8)
	t4.add_child(sc_ops)
	var reset_sc_btn := Button.new()
	reset_sc_btn.text = "恢复默认快捷键"
	reset_sc_btn.pressed.connect(_reset_shortcut_defaults)
	sc_ops.add_child(reset_sc_btn)

	_add_hint_label(t4, "提示：无麦克风时可将麦克风快捷键设为 disabled，直接使用「文字输入框」全局热键（默认 option+space）随处打字唤醒启明星。")
	_add_hint_label(t4, "权限说明：在系统其他应用前置时使用全局快捷键，需在「系统设置 → 隐私与安全性 → 辅助功能 / 输入监控」允许终端或 Python 监听输入；即使未授权，当点击启明星聚焦窗口时也可用本地快捷键。")
	_add_section_label(t4, "鼠标与窗口快捷操作一览")
	_add_hint_label(t4, "• 左键单击启明星：快速打开/收起文字输入框\n• 右键角色 / 顶部状态栏图标：打开快捷菜单\n• 鼠标滚轮上下滚动：即时调整角色大小 (50% - 130%)\n• 拖动至屏幕左/右/上边缘松手：贴边隐藏（鼠标靠近边缘自动滑出）\n• 拖入文件/文件夹到角色身上：直接挂载上下文")

	# --- Tab 5: MCP 服务 ---
	var tab5_scroll := ScrollContainer.new()
	tab5_scroll.name = "MCP 服务"
	tabs.add_child(tab5_scroll)

	var t5 := VBoxContainer.new()
	t5.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t5.add_theme_constant_override("separation", 7)
	tab5_scroll.add_child(t5)

	_add_section_label(t5, "已配置 MCP 服务（支持远程 OAuth 2.1 / SSE / 本地 stdio）")
	_mcp_list = ItemList.new()
	_mcp_list.custom_minimum_size = Vector2(0, 88)
	_mcp_list.item_selected.connect(_on_mcp_item_selected)
	t5.add_child(_mcp_list)

	var mcp_name_row := HBoxContainer.new()
	mcp_name_row.add_theme_constant_override("separation", 8)
	t5.add_child(mcp_name_row)
	var mnl := Label.new()
	mnl.text = "服务名称:"
	mnl.custom_minimum_size = Vector2(88, 0)
	mcp_name_row.add_child(mnl)
	_mcp_name_edit = LineEdit.new()
	_mcp_name_edit.placeholder_text = "英文/数字/下划线（例如 t3_code）"
	_mcp_name_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	mcp_name_row.add_child(_mcp_name_edit)

	var mcp_tr_row := HBoxContainer.new()
	mcp_tr_row.add_theme_constant_override("separation", 8)
	t5.add_child(mcp_tr_row)
	var mtl := Label.new()
	mtl.text = "传输方式:"
	mtl.custom_minimum_size = Vector2(88, 0)
	mcp_tr_row.add_child(mtl)
	_mcp_transport_opt = OptionButton.new()
	_mcp_transport_opt.add_item("streamable_http（远程 HTTP MCP，推荐）", 0)
	_mcp_transport_opt.set_item_metadata(0, "streamable_http")
	_mcp_transport_opt.add_item("sse（远程 SSE MCP）", 1)
	_mcp_transport_opt.set_item_metadata(1, "sse")
	_mcp_transport_opt.add_item("stdio（本地命令行 MCP）", 2)
	_mcp_transport_opt.set_item_metadata(2, "stdio")
	_mcp_transport_opt.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_mcp_transport_opt.item_selected.connect(func(_idx: int) -> void: _update_mcp_field_visibility())
	mcp_tr_row.add_child(_mcp_transport_opt)

	_mcp_auth_row = HBoxContainer.new()
	_mcp_auth_row.add_theme_constant_override("separation", 8)
	t5.add_child(_mcp_auth_row)
	var mal := Label.new()
	mal.text = "认证方式:"
	mal.custom_minimum_size = Vector2(88, 0)
	_mcp_auth_row.add_child(mal)
	_mcp_auth_opt = OptionButton.new()
	_mcp_auth_opt.add_item("oauth（OAuth 2.1 动态注册浏览器登录，如 T3 Code）", 0)
	_mcp_auth_opt.set_item_metadata(0, "oauth")
	_mcp_auth_opt.add_item("none（无认证或自定义 Header）", 1)
	_mcp_auth_opt.set_item_metadata(1, "none")
	_mcp_auth_opt.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_mcp_auth_row.add_child(_mcp_auth_opt)

	_mcp_url_row = HBoxContainer.new()
	_mcp_url_row.add_theme_constant_override("separation", 8)
	t5.add_child(_mcp_url_row)
	var mul := Label.new()
	mul.text = "远程 URL:"
	mul.custom_minimum_size = Vector2(88, 0)
	_mcp_url_row.add_child(mul)
	_mcp_url_edit = LineEdit.new()
	_mcp_url_edit.placeholder_text = "https://.../mcp"
	_mcp_url_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_mcp_url_row.add_child(_mcp_url_edit)

	_mcp_cmd_row = HBoxContainer.new()
	_mcp_cmd_row.add_theme_constant_override("separation", 8)
	t5.add_child(_mcp_cmd_row)
	var mcl := Label.new()
	mcl.text = "启动命令:"
	mcl.custom_minimum_size = Vector2(88, 0)
	_mcp_cmd_row.add_child(mcl)
	_mcp_cmd_edit = LineEdit.new()
	_mcp_cmd_edit.placeholder_text = "例如 npx 或 uvx"
	_mcp_cmd_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_mcp_cmd_row.add_child(_mcp_cmd_edit)

	_mcp_args_row = HBoxContainer.new()
	_mcp_args_row.add_theme_constant_override("separation", 8)
	t5.add_child(_mcp_args_row)
	var mgl := Label.new()
	mgl.text = "命令参数:"
	mgl.custom_minimum_size = Vector2(88, 0)
	_mcp_args_row.add_child(mgl)
	_mcp_args_edit = LineEdit.new()
	_mcp_args_edit.placeholder_text = "空格分隔参数，例如 -y @modelcontextprotocol/server-filesystem ~/Desktop"
	_mcp_args_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_mcp_args_row.add_child(_mcp_args_edit)

	var mcp_safe_row := HBoxContainer.new()
	mcp_safe_row.add_theme_constant_override("separation", 8)
	t5.add_child(mcp_safe_row)
	var msl := Label.new()
	msl.text = "只读白名单:"
	msl.custom_minimum_size = Vector2(88, 0)
	mcp_safe_row.add_child(msl)
	_mcp_safe_tools_edit = LineEdit.new()
	_mcp_safe_tools_edit.placeholder_text = "可选，逗号分隔免审批只读工具名"
	_mcp_safe_tools_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	mcp_safe_row.add_child(_mcp_safe_tools_edit)

	_mcp_enabled_check = CheckButton.new()
	_mcp_enabled_check.text = "启用该 MCP 服务"
	_mcp_enabled_check.button_pressed = true
	t5.add_child(_mcp_enabled_check)

	var mcp_btns := HBoxContainer.new()
	mcp_btns.add_theme_constant_override("separation", 6)
	t5.add_child(mcp_btns)
	var mcp_new_btn := Button.new()
	mcp_new_btn.text = "＋ 新建"
	mcp_new_btn.pressed.connect(_clear_mcp_editor)
	mcp_btns.add_child(mcp_new_btn)
	var mcp_save_btn := Button.new()
	mcp_save_btn.text = "保存并连接"
	mcp_save_btn.pressed.connect(_on_mcp_save_pressed)
	mcp_btns.add_child(mcp_save_btn)
	var mcp_auth_btn := Button.new()
	mcp_auth_btn.text = "🔐 OAuth 授权"
	mcp_auth_btn.pressed.connect(_on_mcp_authorize_pressed)
	mcp_btns.add_child(mcp_auth_btn)
	var mcp_ref_btn := Button.new()
	mcp_ref_btn.text = "刷新"
	mcp_ref_btn.pressed.connect(_fetch_mcp_servers)
	mcp_btns.add_child(mcp_ref_btn)
	var mcp_del_btn := Button.new()
	mcp_del_btn.text = "删除"
	mcp_del_btn.pressed.connect(_on_mcp_delete_pressed)
	mcp_btns.add_child(mcp_del_btn)

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
	var open_url_btn := Button.new()
	open_url_btn.text = "浏览器打开"
	open_url_btn.pressed.connect(func() -> void:
		if _mcp_auth_url_edit.text.strip_edges() != "":
			OS.shell_open(_mcp_auth_url_edit.text.strip_edges())
	)
	_mcp_auth_url_row.add_child(open_url_btn)

	_mcp_tools_label = _add_hint_label(t5, "提示：添加支持 OAuth 2.1 动态注册的远程 MCP（如 T3 Code）后，先点「保存并连接」，再点「🔐 OAuth 授权」在浏览器完成登录即可自动挂载工具。")
	_update_mcp_field_visibility()

	# Footer bar
	var footer := HBoxContainer.new()
	footer.add_theme_constant_override("separation", 8)
	root_col.add_child(footer)

	_status_label = Label.new()
	_status_label.text = "就绪"
	_status_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_status_label.add_theme_font_size_override("font_size", 11)
	_status_label.add_theme_color_override("font_color", Color(0.68, 0.84, 0.96))
	footer.add_child(_status_label)

	var close_btn := Button.new()
	close_btn.text = "关闭"
	close_btn.pressed.connect(func() -> void: visible = false)
	footer.add_child(close_btn)

	_save_btn = Button.new()
	_save_btn.text = "保存并生效"
	_save_btn.pressed.connect(_on_save_pressed)
	footer.add_child(_save_btn)


func _add_hotkey_row(parent: Control, label_text: String, presets: Array) -> OptionButton:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 8)
	parent.add_child(row)
	var lbl := Label.new()
	lbl.text = label_text
	lbl.custom_minimum_size = Vector2(180, 0)
	row.add_child(lbl)
	var opt := OptionButton.new()
	opt.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	for idx in range(presets.size()):
		var raw_val := str(presets[idx])
		var disp := "disabled（禁用）" if raw_val == "disabled" else raw_val
		opt.add_item(disp, idx)
		opt.set_item_metadata(idx, raw_val)
	row.add_child(opt)
	return opt


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
	opt.add_item(target, new_idx)
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
	_status_label.text = "已填入默认快捷键，请点击「保存并生效」应用。"


func _fetch_backend_settings() -> void:
	_pending_mode = "get"
	_status_label.text = "正在同步设置…"
	var err := _http.request("%s/api/config/settings" % http_base, [], HTTPClient.METHOD_GET)
	if err != OK:
		_status_label.text = "读取配置失败 (%d)" % err


func _on_http_completed(_result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	var txt := body.get_string_from_utf8()
	var parsed: Variant = JSON.parse_string(txt)
	if code < 200 or code >= 300 or typeof(parsed) != TYPE_DICTIONARY:
		_status_label.text = "请求异常 HTTP %d" % code
		_save_btn.disabled = false
		return

	var dict: Dictionary = parsed
	if _pending_mode == "get":
		_apply_loaded_settings(dict)
		_status_label.text = "已读取脱敏配置（密钥以明文安全隐藏）"
	elif _pending_mode == "post":
		_save_btn.disabled = false
		_ds_key_edit.text = ""
		_prof_key_edit.text = ""
		var s_dict: Dictionary = dict.get("settings", {})
		if not s_dict.is_empty():
			_apply_loaded_settings(s_dict)
		_status_label.text = "保存成功，已写入 config.yaml (0600) 并热更新生效！"


func _apply_loaded_settings(data: Dictionary) -> void:
	_loaded_settings = data
	var ds_info: Dictionary = data.get("shared_deepseek_key", {})
	if bool(ds_info.get("has_key", false)):
		_ds_key_status.text = "当前 DeepSeek 密钥: %s（留空则保持不变）" % str(ds_info.get("masked", "已配置"))
	else:
		_ds_key_status.text = "当前状态: 尚未配置 DeepSeek API Key"

	var profiles: Dictionary = data.get("profiles", {})
	_profile_names.clear()
	_default_profile_opt.clear()
	_prof_select_opt.clear()

	var def_prof := str(data.get("default_profile", ""))
	var def_idx := 0
	var idx := 0
	for pname in profiles.keys():
		var p: Dictionary = profiles[pname]
		var lbl := str(p.get("label", pname))
		_profile_names.append(str(pname))
		_default_profile_opt.add_item("%s (%s)" % [pname, lbl], idx)
		_prof_select_opt.add_item(str(pname), idx)
		if str(pname) == def_prof:
			def_idx = idx
		idx += 1
	if not _profile_names.is_empty():
		_default_profile_opt.select(def_idx)
		_prof_select_opt.select(def_idx)
		_on_profile_selected(def_idx)

	var audio_sec: Dictionary = data.get("audio", {})
	_audio_enabled_check.button_pressed = bool(audio_sec.get("enabled", true))
	var tts_sec: Dictionary = audio_sec.get("tts", {})
	var v := str(tts_sec.get("voice", "stella"))
	var v_idx := VOICE_OPTIONS.find(v)
	_tts_voice_opt.select(v_idx if v_idx >= 0 else 0)

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
	_auto_hide_check.button_pressed = bool(godot_sec.get("auto_hide_on_capture", true))


func _on_profile_selected(idx: int) -> void:
	if idx < 0 or idx >= _profile_names.size():
		return
	var pname := _profile_names[idx]
	var profiles: Dictionary = _loaded_settings.get("profiles", {})
	var p: Dictionary = profiles.get(pname, {})
	_prof_model_edit.text = str(p.get("model", ""))
	_prof_url_edit.text = str(p.get("base_url", ""))
	_prof_key_edit.text = ""
	if bool(p.get("has_key", false)):
		_prof_key_status.text = "该 Profile 已配置密钥: %s" % str(p.get("key_masked", "****"))
	else:
		_prof_key_status.text = "该 Profile 暂无独立密钥"


func _on_save_pressed() -> void:
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
			_status_label.text = "⚠️ 快捷键冲突: 「%s」被重复分配，请修改后再保存！" % hk
			return
		seen_hotkeys[hk] = true

	var new_scale := _scale_slider.value / 100.0
	var roam_on := _roam_check.button_pressed
	var earcons_on := _earcons_check.button_pressed
	var ambient_on := _ambient_check.button_pressed
	var auto_hide := _auto_hide_check.button_pressed
	prefs_applied.emit(new_scale, roam_on, earcons_on, ambient_on, auto_hide)

	var payload: Dictionary = {
		"audio": {
			"enabled": _audio_enabled_check.button_pressed,
			"tts": {"voice": VOICE_OPTIONS[clampi(_tts_voice_opt.selected, 0, VOICE_OPTIONS.size() - 1)]},
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
	}

	if _default_profile_opt.selected >= 0 and _default_profile_opt.selected < _profile_names.size():
		payload["default_profile"] = _profile_names[_default_profile_opt.selected]

	var ds_key := _ds_key_edit.text.strip_edges()
	if ds_key != "":
		payload["shared_deepseek_api_key"] = ds_key

	if _prof_select_opt.selected >= 0 and _prof_select_opt.selected < _profile_names.size():
		var sel_name := _profile_names[_prof_select_opt.selected]
		var prof_patch: Dictionary = {
			"model": _prof_model_edit.text.strip_edges(),
			"base_url": _prof_url_edit.text.strip_edges(),
		}
		var pk := _prof_key_edit.text.strip_edges()
		if pk != "":
			prof_patch["api_key"] = pk
		payload["profiles"] = {sel_name: prof_patch}

	_pending_mode = "post"
	_save_btn.disabled = true
	_status_label.text = "正在保存并应用设置…"
	var err := _http.request(
		"%s/api/config/settings" % http_base,
		["Content-Type: application/json"],
		HTTPClient.METHOD_POST,
		JSON.stringify(payload)
	)
	if err != OK:
		_save_btn.disabled = false
		_status_label.text = "保存请求失败 (%d)" % err


# --- Tab 5: MCP 服务管理与 OAuth 授权 ---

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
	_mcp_name_edit.text = ""
	_mcp_transport_opt.select(0)
	_mcp_auth_opt.select(0)
	_mcp_url_edit.text = ""
	_mcp_cmd_edit.text = ""
	_mcp_args_edit.text = ""
	_mcp_safe_tools_edit.text = ""
	_mcp_enabled_check.button_pressed = true
	_mcp_auth_url_row.visible = false
	_mcp_auth_url_edit.text = ""
	_update_mcp_field_visibility()
	_status_label.text = "已清空编辑器，填写后点击「保存并连接」即可新建 MCP"


func _fetch_mcp_servers() -> void:
	if _mcp_http == null:
		return
	_mcp_pending_op = "list"
	_mcp_http.cancel_request()
	_mcp_http.request("%s/api/mcp/servers" % http_base, [], HTTPClient.METHOD_GET)


func _on_mcp_item_selected(idx: int) -> void:
	if idx < 0 or idx >= _mcp_servers.size():
		return
	var d_var: Variant = _mcp_servers[idx]
	if typeof(d_var) != TYPE_DICTIONARY:
		return
	var d: Dictionary = d_var
	_mcp_name_edit.text = str(d.get("name", ""))
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
	if typeof(args_arr) == TYPE_ARRAY:
		_mcp_args_edit.text = " ".join(PackedStringArray(args_arr))
	else:
		_mcp_args_edit.text = ""
	var safe_arr: Variant = d.get("safe_tools", [])
	if typeof(safe_arr) == TYPE_ARRAY:
		_mcp_safe_tools_edit.text = ", ".join(PackedStringArray(safe_arr))
	else:
		_mcp_safe_tools_edit.text = ""
	_mcp_enabled_check.button_pressed = bool(d.get("enabled", true))
	_update_mcp_field_visibility()
	_render_mcp_detail(d)


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
		summary += " | 已挂载 %d 个工具: %s" % [tools_arr.size(), ", ".join(t_names)]
	elif err != "":
		summary += " | %s" % err
	_mcp_tools_label.text = summary


func _apply_mcp_servers_list(servers: Array) -> void:
	var prev_name := _mcp_name_edit.text.strip_edges()
	_mcp_servers = servers
	_mcp_list.clear()
	var sel_idx := -1
	for i in range(servers.size()):
		var item_var: Variant = servers[i]
		if typeof(item_var) != TYPE_DICTIONARY:
			continue
		var d: Dictionary = item_var
		var sname := str(d.get("name", ""))
		var tr := str(d.get("transport", "stdio"))
		var st := str(d.get("status", "offline"))
		var tools: Variant = d.get("tools", [])
		var tcount := (tools as Array).size() if typeof(tools) == TYPE_ARRAY else 0
		var icon := "⚪"
		if st == "connected":
			icon = "🟢"
		elif st == "needs_auth":
			icon = "🔐"
		elif st == "offline":
			icon = "🔴"
		_mcp_list.add_item("%s %s (%s | %d 工具 | %s)" % [icon, sname, tr, tcount, st])
		if sname == prev_name:
			sel_idx = _mcp_list.item_count - 1
	if sel_idx >= 0:
		_mcp_list.select(sel_idx)
		_render_mcp_detail(_mcp_servers[sel_idx])
	elif not servers.is_empty() and prev_name == "":
		_mcp_list.select(0)
		_on_mcp_item_selected(0)


func _build_mcp_payload() -> Dictionary:
	var tr := str(_mcp_transport_opt.get_item_metadata(_mcp_transport_opt.selected))
	var am := str(_mcp_auth_opt.get_item_metadata(_mcp_auth_opt.selected))
	return {
		"name": _mcp_name_edit.text.strip_edges(),
		"enabled": _mcp_enabled_check.button_pressed,
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
		_status_label.text = "⚠️ 请填写 MCP 服务名称（如 t3_code）"
		return
	_mcp_pending_op = "save"
	_status_label.text = "正在保存并连接 MCP 服务「%s」…" % str(payload["name"])
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
		_status_label.text = "⚠️ 请先填写并保存需要 OAuth 授权的 MCP 名称"
		return
	_mcp_pending_op = "authorize"
	_mcp_auth_url_row.visible = false
	_status_label.text = "正在向「%s」发起 OAuth 2.1 动态注册与授权…" % sname
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
	_status_label.text = "正在删除 MCP「%s」…" % sname
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
		_status_label.text = "MCP 请求失败 (%d)%s" % [code, (": " + detail) if detail != "" else ""]
		return

	var dict: Dictionary = parsed
	var srv_arr: Variant = dict.get("servers", [])
	if typeof(srv_arr) == TYPE_ARRAY:
		_apply_mcp_servers_list(srv_arr)

	if _mcp_pending_op == "save":
		_status_label.text = "已保存 MCP 配置并尝试连接；若需 OAuth 登录请点「🔐 OAuth 授权」"
	elif _mcp_pending_op == "delete":
		_clear_mcp_editor()
		_status_label.text = "已删除该 MCP 配置并清理授权缓存"
	elif _mcp_pending_op == "authorize":
		var st := str(dict.get("status", ""))
		var auth_url := str(dict.get("auth_url", ""))
		if st == "authorizing" and auth_url != "":
			_mcp_auth_url_edit.text = auth_url
			_mcp_auth_url_row.visible = true
			_mcp_tools_label.text = "请在浏览器中批准登录（授权完成后将自动连接并同步工具）"
			_status_label.text = "已打开浏览器 OAuth 授权页，等待回调中…"
			_mcp_poll_ticks = 0
			_mcp_poll_timer.start()
		elif st == "connected":
			_mcp_poll_timer.stop()
			_mcp_auth_url_row.visible = false
			_status_label.text = "MCP 授权并连接成功！"
		else:
			_mcp_poll_timer.stop()
			_status_label.text = "OAuth 授权失败: %s" % str(dict.get("error", "未知错误"))
	elif _mcp_pending_op == "list":
		var cur_name := _mcp_name_edit.text.strip_edges()
		for item_var in _mcp_servers:
			if typeof(item_var) == TYPE_DICTIONARY and str((item_var as Dictionary).get("name", "")) == cur_name:
				if str((item_var as Dictionary).get("status", "")) == "connected":
					_mcp_poll_timer.stop()
					_mcp_auth_url_row.visible = false
					_status_label.text = "MCP「%s」已授权连接成功，工具已挂载！" % cur_name
				break
