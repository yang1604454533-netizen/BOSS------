# BOSS直聘岗位采集助手（图形界面版）
# 核心：页面操作 + 接口监听，附带 customtkinter 现代风格界面
import os
import sys
import shutil
import subprocess
import time
import re
import json
import html
import datetime
import csv
import queue
import threading
import traceback
import urllib.request
from tkinter import filedialog, messagebox
from urllib.parse import quote

from DrissionPage import ChromiumPage, ChromiumOptions

import customtkinter as ctk

# 脚本所在目录，导出的CSV都保存到这里，避免运行时找不到文件
# 打包成 exe 后 __file__ 指向临时解压目录，改用 exe 所在目录，保证导出的文件落在用户能看到的地方
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 设置文件：保存导出目录等用户偏好
SETTINGS_FILE = os.path.join(BASE_DIR, '设置.json')
# 运行日志目录：按日期滚动保存，避免历史日志被覆盖（不再使用固定的 '运行日志.txt'）
LOG_DIR = os.path.join(BASE_DIR, 'logs')
LOG_FILE = os.path.join(LOG_DIR, f'运行日志_{datetime.date.today().strftime("%Y%m%d")}.txt')

# ==================== 运行参数（集中管理魔法数字） ====================
CHROME_DEBUG_ADDR = '127.0.0.1:9222'   # 调试模式浏览器的地址（Chrome/Edge 均为 Chromium 内核，端口一致）
# 支持的浏览器：均为 Chromium 内核，通过 CDP 连接调试端口，Chrome 与 Edge 通用。
# label=界面显示名，exe=命令行启动所用的可执行文件名，name=日志中显示的名称。
BROWSERS = {
    'chrome': {'label': 'Chrome 谷歌浏览器', 'exe': 'chrome.exe', 'name': 'Chrome'},
    'edge':   {'label': 'Edge 微软浏览器',   'exe': 'msedge.exe', 'name': 'Edge'},
}


def detect_browser():
    """检测本机可用的浏览器：优先 Edge，Edge 不存在时回退 Chrome；都没有则默认按 Edge 处理。

    依次检查：常见安装路径 -> 系统 PATH -> 注册表 App Paths。
    返回 BROWSERS 中的 key（'edge' 或 'chrome'）。"""
    install_paths = {
        'edge': [
            r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        ],
        'chrome': [
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        ],
    }
    # 1. 常见安装路径
    for key in ('edge', 'chrome'):
        for path in install_paths[key]:
            if os.path.isfile(path):
                return key
    # 2. 系统 PATH
    for key, exe in (('edge', 'msedge.exe'), ('chrome', 'chrome.exe')):
        if shutil.which(exe):
            return key
    # 3. 注册表 App Paths
    try:
        import winreg
        for key, exe in (('edge', 'msedge.exe'), ('chrome', 'chrome.exe')):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}') as k:
                    path = winreg.QueryValue(k, None)
                    if path and os.path.isfile(path):
                        return key
            except OSError:
                continue
    except Exception:
        pass
    # 4. 兜底：默认 Edge（若实际未安装，采集连接失败时会给出对应启动命令提示）
    return 'edge'


def _browser_exe_path(browser_key):
    """返回浏览器可执行文件的完整路径，找不到返回 None"""
    install_paths = {
        'edge': [
            r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        ],
        'chrome': [
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        ],
    }
    for p in install_paths.get(browser_key, []):
        if os.path.isfile(p):
            return p
    return shutil.which(BROWSERS[browser_key]['exe'])


def _debug_browser_ready():
    """检查 127.0.0.1:9222 是否已有调试模式浏览器在监听"""
    try:
        with urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _move_browser_window(visible):
    """调整调试浏览器窗口位置：visible=True 移到可见区域，False 移到屏幕外隐藏。

    通过 CDP 控制窗口位置，让用户无感：采集时浏览器在屏幕外运行，只看到软件界面结果。"""
    try:
        co = ChromiumOptions()
        co.debugger_address = CHROME_DEBUG_ADDR
        dp = ChromiumPage(co)
        if visible:
            try:
                dp.set.window.normal()
                dp.set.window.location(80, 80)
            except Exception:
                pass
        else:
            try:
                dp.set.window.location(-32000, -32000)
            except Exception:
                pass
    except Exception:
        pass


def ensure_debug_browser(visible=False):
    """确保调试模式浏览器已启动并调整到目标显示状态。

    visible=False（默认）：浏览器在屏幕外运行，用户无感，只看到软件界面的采集结果。
    visible=True：浏览器显示到可见区域（用于首次登录）。
    独立用户数据目录放在软件目录下（browser_profile_xxx），登录态会保存并复用。
    返回 (是否成功, 提示信息)。"""
    launched = False
    browser_key = detect_browser()
    if not _debug_browser_ready():
        exe_path = _browser_exe_path(browser_key)
        if not exe_path:
            return False, (f'未找到 {BROWSERS[browser_key]["name"]}，请手动运行：'
                           f'{BROWSERS[browser_key]["exe"]} --remote-debugging-port=9222')
        profile_dir = os.path.join(BASE_DIR, f'browser_profile_{browser_key}')
        try:
            os.makedirs(profile_dir, exist_ok=True)
        except Exception:
            profile_dir = None
        # 先放到屏幕外启动，避免窗口闪现
        cmd = [exe_path, '--remote-debugging-port=9222', '--no-first-run',
               '--no-default-browser-check', '--window-position=-32000,-32000',
               '--window-size=800,600']
        if profile_dir:
            cmd.append(f'--user-data-dir={profile_dir}')
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return False, f'启动浏览器失败：{e}'
        for _ in range(50):
            time.sleep(0.2)
            if _debug_browser_ready():
                launched = True
                break
        if not launched:
            return False, (f'{BROWSERS[browser_key]["name"]} 启动超时，请手动运行：'
                           f'{BROWSERS[browser_key]["exe"]} --remote-debugging-port=9222')
    # 无论是否刚启动，都调整窗口显示状态
    _move_browser_window(visible)
    if launched:
        mode = '显示' if visible else '后台隐藏'
        return True, f'已自动启动 {BROWSERS[browser_key]["name"]}（{mode}）'
    return True, ('已显示浏览器窗口' if visible else '浏览器在后台隐藏运行')


def _fit_optionmenu(menu):
    """收紧 CTkOptionMenu 弹出菜单的最小宽度，让菜单贴合选项文字。

    customtkinter 默认 min_character_width=18，选项短时右侧会留出大片空白、
    滚动条也离文字很远；改成 8 后菜单宽度随最长选项自适应（长选项不会被截断）。"""
    try:
        menu._dropdown_menu.configure(min_character_width=8)
    except Exception:
        pass


CITY_FETCH_TIMEOUT = 10                 # 拉取城市数据接口的超时秒数
PAGE_SLEEP_SECONDS = 5                  # 翻页之间的等待秒数
JOBLIST_TIMEOUT = 5                     # 监听岗位列表接口的超时秒数
DESC_FETCH_TIMEOUT = 8                  # 抓取职位描述时页面加载超时秒数
DESC_FETCH_DELAY = 1                    # 每条职位描述抓取后的间隔秒数（降低频率，减少风控）
DESC_FETCH_BATCH = 30                   # 每页最多抓取职位描述的条数上限
POLL_INTERVAL_MS = 100                  # 消息队列轮询间隔（毫秒）
KEYWORD_DEBOUNCE_MS = 400               # 关键词输入防抖延时（毫秒）
FILTER_APPLY_DELAY_MS = 10              # 筛选变化后延迟重建列表的毫秒数（避免事件回调中重建卡顿）
MULTI_POPUP_HEIGHT = 260                # 多选下拉面板高度

# ==================== 界面主题色 ====================
# BOSS直聘 用蓝色系；前程无忧(51job) 用品牌橙色 FF6314。
# 切换采集网站时，界面上涉及蓝色的部分（标题、主要按钮、下拉框、选中行高亮）会整体切换配色。
# primary=主色（标题文字/按钮/选中高亮），hover=悬停/加深色。
THEME_COLORS = {
    'boss': {'primary': '#1f6aa5', 'hover': '#144870'},
    '51job': {'primary': '#ff6314', 'hover': '#d9540f'},
}

# ==================== 城市下拉框 ====================
# 下拉框只展示这一二线城市前 20，其余城市可在右侧输入框手动输入城市名。
TOP_CITY_NAMES = [
    '北京', '上海', '广州', '深圳',       # 一线
    '成都', '杭州', '重庆', '西安', '苏州',  # 新一线
    '武汉', '南京', '天津', '郑州', '长沙',
    '东莞', '沈阳', '昆明', '青岛', '宁波',
    '合肥',                              # 二线
]


def load_settings():
    """读取用户设置（如导出目录），失败返回空字典"""
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings):
    """保存用户设置（如导出目录）"""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ==================== 基础工具 ====================

def log(msg):
    """带时间戳的日志输出，方便排查问题"""
    print(f'[{datetime.datetime.now():%H:%M:%S}] {msg}')


def open_csv_retry(base_name, target_dir=None):
    """尝试打开CSV文件；若文件正被其他程序占用（如用Excel打开），自动换带序号的新文件名。
    target_dir: 保存目录（默认使用脚本所在目录）
    每次重试都基于原始文件名加序号，避免文件名无限累积。
    返回 (文件对象, 最终使用的文件完整路径)"""
    base = os.path.join(target_dir or BASE_DIR, base_name)
    file_index = 1
    while True:
        # 第1次用原始文件名，之后用 原始名_序号.csv
        name = base if file_index == 1 else re.sub(r'\.csv$', f'_{file_index}.csv', base)
        try:
            return open(name, mode='w', encoding='utf-8-sig', newline=''), name
        except PermissionError:
            log(f'文件 {name} 正被其他程序占用（可能用Excel打开了它），改用新文件名继续...')
            file_index += 1
            if file_index > 999:
                raise


def parse_month_salary(s):
    if not s or "面议" in s:
        return None
    # 忽略按天/周薪
    if "元/天" in s or "元/周" in s:
        return None
    # 格式1：15K-25K（带薪级单位，如 15K-25K 或 15K·13薪 之类）
    m = re.search(r"(\d+(?:\.\d+)?)\s*K\s*-\s*(\d+(?:\.\d+)?)\s*K", s, re.I)
    if m:
        low = float(m.group(1)) * 1000
        high = float(m.group(2)) * 1000
        return (low + high) / 2
    # 格式2：15-25K（两组数字间为 - 或 ~，末尾带 K）
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*K", s, re.I)
    if m:
        low = float(m.group(1)) * 1000
        high = float(m.group(2)) * 1000
        return (low + high) / 2
    # 格式3：8000-12000元/月（纯数字区间带"元/月"）
    m = re.search(r"(\d+)\s*[-~]\s*(\d+)\s*元", s)
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        return (low + high) / 2
    # 格式4：1.5-2.5万（以"万"为单位的区间）
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*万", s)
    if m:
        low = float(m.group(1)) * 10000
        high = float(m.group(2)) * 10000
        return (low + high) / 2
    return None


def parse_51job_salary(s):
    """解析前程无忧（51job）的薪资描述，返回月薪数值，无法解析返回 None。

    51job 常见格式：'8千-1.2万/月'、'1-1.5万/月'、'8千-12千/月'、'20-30万/年'、'8千/月'、'1万/月' 等。
    年薪会折算为月薪，以复用统一的薪资分档（10K/15K/20K/30K）。"""
    if not s or '面议' in s:
        return None
    # 忽略按天/时薪
    if '元/天' in s or '元/时' in s or '元/小时' in s:
        return None

    def to_yuan(v, unit):
        if unit == '千':
            return v * 1000
        if unit == '万':
            return v * 10000
        return v

    # 区间：如 "8千-1.2万" / "1-1.5万" / "8千-12千"（任一侧单位省略时沿用另一侧单位；支持 - ~ 至 分隔）
    m = re.search(r'([\d.]+)\s*(千|万)?\s*[-~至]\s*([\d.]+)\s*(千|万)?', s)
    if m:
        unit_low = m.group(2)
        unit_high = m.group(4)
        if unit_high and not unit_low:
            unit_low = unit_high
        if unit_low and not unit_high:
            unit_high = unit_low
        low = to_yuan(float(m.group(1)), unit_low)
        high = to_yuan(float(m.group(3)), unit_high)
        avg = (low + high) / 2
        if '年' in s and '月' not in s:
            avg /= 12
        return avg

    # 单一数值：如 "8千/月" / "1万/月"
    m = re.search(r'([\d.]+)\s*(千|万)\s*/\s*月', s)
    if m:
        v = float(m.group(1))
        return v * 1000 if m.group(2) == '千' else v * 10000
    return None


# ==================== 爬虫核心 ====================

# 热门城市代码（内置兜底；启动时会尝试自动加载全国城市）
CITY_OPTIONS = {
    '北京': '101010100',
    '上海': '101020100',
    '广州': '101280100',
    '深圳': '101280600',
    '杭州': '101210100',
    '南京': '101190100',
    '成都': '101270100',
    '武汉': '101200100',
    '西安': '101110100',
    '苏州': '101190400',
}

# 前程无忧（51job）城市代码（jobArea 参数），与 BOSS 直聘城市代码体系不同
CITY_51JOB_OPTIONS = {
    '北京': '010000',
    '上海': '020000',
    '广州': '030200',
    '惠州': '030300',
    '汕头': '030400',
    '珠海': '030500',
    '佛山': '030600',
    '中山': '030700',
    '东莞': '030800',
    '韶关': '031400',
    '江门': '031500',
    '湛江': '031700',
    '肇庆': '031800',
    '清远': '031900',
    '潮州': '032000',
    '河源': '032100',
    '揭阳': '032200',
    '茂名': '032300',
    '汕尾': '032400',
    '梅州': '032600',
    '开平': '032700',
    '阳江': '032800',
    '云浮': '032900',
    '深圳': '040000',
    '天津': '050000',
    '重庆': '060000',
    '南京': '070200',
    '苏州': '070300',
    '无锡': '070400',
    '常州': '070500',
    '昆山': '070600',
    '常熟': '070700',
    '扬州': '070800',
    '南通': '070900',
    '镇江': '071000',
    '徐州': '071100',
    '连云港': '071200',
    '盐城': '071300',
    '张家港': '071400',
    '太仓': '071600',
    '泰州': '071800',
    '淮安': '071900',
    '宿迁': '072000',
    '杭州': '080200',
    '宁波': '080300',
    '温州': '080400',
    '绍兴': '080500',
    '金华': '080600',
    '嘉兴': '080700',
    '台州': '080800',
    '湖州': '080900',
    '丽水': '081000',
    '舟山': '081100',
    '衢州': '081200',
    '义乌': '081400',
    '海宁': '081600',
    '成都': '090200',
    '绵阳': '090300',
    '乐山': '090400',
    '泸州': '090500',
    '德阳': '090600',
    '宜宾': '090700',
    '自贡': '090800',
    '内江': '090900',
    '攀枝花': '091000',
    '南充': '091100',
    '眉山': '091200',
    '广安': '091300',
    '资阳': '091400',
    '遂宁': '091500',
    '广元': '091600',
    '达州': '091700',
    '雅安': '091800',
    '西昌': '091900',
    '巴中': '092000',
    '甘孜': '092100',
    '阿坝': '092200',
    '凉山': '092300',
    '海口': '100200',
    '三亚': '100300',
    '文昌': '100500',
    '琼海': '100600',
    '万宁': '100700',
    '儋州': '100800',
    '东方': '100900',
    '五指山': '101000',
    '定安': '101100',
    '屯昌': '101200',
    '澄迈': '101300',
    '临高': '101400',
    '三沙': '101500',
    '琼中': '101600',
    '保亭': '101700',
    '白沙': '101800',
    '昌江': '101900',
    '乐东': '102000',
    '陵水': '102100',
    '福州': '110200',
    '厦门': '110300',
    '泉州': '110400',
    '漳州': '110500',
    '莆田': '110600',
    '三明': '110700',
    '南平': '110800',
    '宁德': '110900',
    '龙岩': '111000',
    '济南': '120200',
    '青岛': '120300',
    '烟台': '120400',
    '潍坊': '120500',
    '威海': '120600',
    '淄博': '120700',
    '临沂': '120800',
    '济宁': '120900',
    '东营': '121000',
    '泰安': '121100',
    '日照': '121200',
    '德州': '121300',
    '菏泽': '121400',
    '滨州': '121500',
    '枣庄': '121600',
    '聊城': '121700',
    '南昌': '130200',
    '九江': '130300',
    '景德镇': '130400',
    '萍乡': '130500',
    '新余': '130600',
    '鹰潭': '130700',
    '赣州': '130800',
    '吉安': '130900',
    '宜春': '131000',
    '抚州': '131100',
    '上饶': '131200',
    '南宁': '140200',
    '桂林': '140300',
    '柳州': '140400',
    '北海': '140500',
    '玉林': '140600',
    '梧州': '140700',
    '防城港': '140800',
    '钦州': '140900',
    '贵港': '141000',
    '百色': '141100',
    '河池': '141200',
    '来宾': '141300',
    '崇左': '141400',
    '贺州': '141500',
    '合肥': '150200',
    '芜湖': '150300',
    '安庆': '150400',
    '马鞍山': '150500',
    '蚌埠': '150600',
    '阜阳': '150700',
    '铜陵': '150800',
    '滁州': '150900',
    '黄山': '151000',
    '淮南': '151100',
    '六安': '151200',
    '宣城': '151400',
    '池州': '151500',
    '宿州': '151600',
    '淮北': '151700',
    '亳州': '151800',
    '石家庄': '160200',
    '廊坊': '160300',
    '保定': '160400',
    '唐山': '160500',
    '秦皇岛': '160600',
    '邯郸': '160700',
    '沧州': '160800',
    '张家口': '160900',
    '承德': '161000',
    '邢台': '161100',
    '衡水': '161200',
    '郑州': '170200',
    '洛阳': '170300',
    '开封': '170400',
    '焦作': '170500',
    '南阳': '170600',
    '新乡': '170700',
    '周口': '170800',
    '安阳': '170900',
    '平顶山': '171000',
    '许昌': '171100',
    '信阳': '171200',
    '商丘': '171300',
    '驻马店': '171400',
    '漯河': '171500',
    '濮阳': '171600',
    '鹤壁': '171700',
    '三门峡': '171800',
    '济源': '171900',
    '邓州': '172000',
    '武汉': '180200',
    '宜昌': '180300',
    '黄石': '180400',
    '襄阳': '180500',
    '十堰': '180600',
    '荆州': '180700',
    '荆门': '180800',
    '孝感': '180900',
    '鄂州': '181000',
    '黄冈': '181100',
    '随州': '181200',
    '咸宁': '181300',
    '仙桃': '181400',
    '潜江': '181500',
    '天门': '181600',
    '神农架': '181700',
    '恩施': '181800',
    '长沙': '190200',
    '株洲': '190300',
    '湘潭': '190400',
    '衡阳': '190500',
    '岳阳': '190600',
    '常德': '190700',
    '益阳': '190800',
    '郴州': '190900',
    '邵阳': '191000',
    '怀化': '191100',
    '娄底': '191200',
    '永州': '191300',
    '张家界': '191400',
    '湘西': '191500',
    '西安': '200200',
    '咸阳': '200300',
    '宝鸡': '200400',
    '铜川': '200500',
    '延安': '200600',
    '渭南': '200700',
    '榆林': '200800',
    '汉中': '200900',
    '安康': '201000',
    '商洛': '201100',
    '杨凌': '201200',
    '太原': '210200',
    '运城': '210300',
    '大同': '210400',
    '临汾': '210500',
    '长治': '210600',
    '晋城': '210700',
    '阳泉': '210800',
    '朔州': '210900',
    '晋中': '211000',
    '忻州': '211100',
    '吕梁': '211200',
    '哈尔滨': '220200',
    '伊春': '220300',
    '绥化': '220400',
    '大庆': '220500',
    '齐齐哈尔': '220600',
    '牡丹江': '220700',
    '佳木斯': '220800',
    '鸡西': '220900',
    '鹤岗': '221000',
    '双鸭山': '221100',
    '黑河': '221200',
    '七台河': '221300',
    '大兴安岭': '221400',
    '沈阳': '230200',
    '大连': '230300',
    '鞍山': '230400',
    '营口': '230500',
    '抚顺': '230600',
    '锦州': '230700',
    '丹东': '230800',
    '葫芦岛': '230900',
    '本溪': '231000',
    '辽阳': '231100',
    '铁岭': '231200',
    '盘锦': '231300',
    '朝阳': '231400',
    '阜新': '231500',
    '长春': '240200',
    '吉林': '240300',
    '辽源': '240400',
    '通化': '240500',
    '四平': '240600',
    '松原': '240700',
    '延吉': '240800',
    '白山': '240900',
    '白城': '241000',
    '延边': '241100',
    '昆明': '250200',
    '曲靖': '250300',
    '玉溪': '250400',
    '大理': '250500',
    '丽江': '250600',
    '红河州': '251000',
    '普洱': '251100',
    '保山': '251200',
    '昭通': '251300',
    '文山': '251400',
    '西双版纳': '251500',
    '德宏': '251600',
    '楚雄': '251700',
    '临沧': '251800',
    '怒江': '251900',
    '迪庆': '252000',
    '贵阳': '260200',
    '遵义': '260300',
    '六盘水': '260400',
    '安顺': '260500',
    '铜仁': '260600',
    '毕节': '260700',
    '黔西南': '260800',
    '黔东南': '260900',
    '黔南': '261000',
    '兰州': '270200',
    '金昌': '270300',
    '嘉峪关': '270400',
    '酒泉': '270500',
    '天水': '270600',
    '武威': '270700',
    '白银': '270800',
    '张掖': '270900',
    '平凉': '271000',
    '定西': '271100',
    '陇南': '271200',
    '庆阳': '271300',
    '临夏': '271400',
    '甘南': '271500',
    '呼和浩特': '280200',
    '赤峰': '280300',
    '包头': '280400',
    '通辽': '280700',
    '鄂尔多斯': '280800',
    '巴彦淖尔': '280900',
    '乌海': '281000',
    '呼伦贝尔': '281100',
    '乌兰察布': '281200',
    '银川': '290200',
    '吴忠': '290300',
    '中卫': '290400',
    '石嘴山': '290500',
    '固原': '290600',
    '拉萨': '300200',
    '日喀则': '300300',
    '林芝': '300400',
    '山南': '300500',
    '昌都': '300600',
    '那曲': '300700',
    '阿里': '300800',
    '乌鲁木齐': '310200',
    '克拉玛依': '310300',
    '伊犁': '310500',
    '阿克苏': '310600',
    '哈密': '310700',
    '石河子': '310800',
    '阿拉尔': '310900',
    '五家渠': '311000',
    '图木舒克': '311100',
    '昌吉': '311200',
    '阿勒泰': '311300',
    '吐鲁番': '311400',
    '塔城': '311500',
    '和田': '311600',
    '克孜勒苏柯尔克孜': '311700',
    '巴音郭楞': '311800',
    '博尔塔拉': '311900',
    '昆玉': '312000',
    '北屯': '312100',
    '铁门关': '312200',
    '可克达拉': '312300',
    '胡杨河': '312400',
    '双河': '312500',
    '新星': '312600',
    '西宁': '320200',
    '海东': '320300',
    '海西': '320400',
    '海北': '320500',
    '黄南': '320600',
    '海南州': '320700',
    '果洛': '320800',
    '玉树': '320900',
    '香港': '330000',
    '澳门': '340000',
    '台湾': '350000',
}

# 常见岗位关键词（下拉选项，也可以自定义输入）
KEYWORD_OPTIONS = [
    '游戏测试', '软件测试', 'Python', 'Java', '前端开发',
    '产品经理', '数据分析', '运营', 'UI设计', '算法工程师',
]


def fetch_all_cities():
    """从BOSS直聘接口拉取城市数据。
    返回 (全部城市dict{城市名:城市代码}, 热门城市名列表, 省份映射{城市名:省名})；失败返回 (None, None, None)"""
    try:
        # 不走系统代理，避免本机代理配置异常导致拉取失败
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            'https://www.zhipin.com/wapi/zpCommon/data/city.json',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.zhipin.com/',
            })
        with opener.open(req, timeout=CITY_FETCH_TIMEOUT) as r:
            data = json.loads(r.read().decode('utf-8'))
        if data.get('code') == 0:
            zp = data.get('zpData', {})
            cities = {}
            province_map = {}
            # 热门城市（下拉框只显示这些，保持简洁）
            hot_names = []
            for city in zp.get('hotCityList', []):
                name = city.get('name')
                if name and name != '全国':
                    hot_names.append(name)
            # 全部城市与省份映射（cityList 为省级列表，subLevelModelList 为城市）
            for province in zp.get('cityList', []):
                pname = province.get('name')
                for city in province.get('subLevelModelList') or []:
                    name = city.get('name')
                    code = city.get('code')
                    if name and code:
                        cities[name] = code
                    if name and pname:
                        province_map[name] = pname
            return (cities if cities else None), hot_names, province_map
    except Exception:
        return None, None, None

def sanitize_csv_value(value):
    """CSV 公式注入防护：字符串以 = + - @ 开头时前面加 '，
    避免用 Excel 打开 CSV 时被当作公式执行（如 =cmd 或 =HYPERLINK）"""
    if isinstance(value, str):
        if value.startswith(('=', '+', '-', '@')):
            return "'" + value
        return value
    if isinstance(value, (list, tuple)):
        return [sanitize_csv_value(v) for v in value]
    return value


CSV_FIELDNAMES = [
    '岗位名称', '公司', '规模', '公司领域', '学历要求',
    '经验要求', '技能需求', '福利待遇', '薪资', '薪资原文',
    '省', '市', '区', '商圈', '经度', '纬度', '岗位链接', 'jobId',
    '职位描述'
]

# 筛选下拉框覆盖的字段（第二行筛选区）
FILTER_FIELDS = [
    '规模', '公司领域', '学历要求',
    '经验要求', '技能需求', '福利待遇', '薪资',
]

# 支持点击多选的筛选字段（弹出勾选窗口）：
# 福利待遇 = 必须包含所有勾选项；公司领域/薪资 = 命中任一勾选项即可
MULTI_SELECT_FIELDS = ['公司领域', '薪资', '福利待遇']

# 省市区筛选字段（第三行，采集到数据后才显示）
LOCATION_FIELDS = ['省', '市', '区']

# 常用筛选选项预设（采集前即可选择；采集后会追加实际数据值）
PRESET_FILTER_OPTIONS = {
    '学历要求': ['学历不限', '大专', '本科', '硕士', '博士'],
    '经验要求': ['经验不限', '在校/应届', '1年以内', '1-3年', '3-5年', '5-10年', '10年以上'],
    '规模': ['0-20人', '20-99人', '100-499人', '500-999人', '1000-9999人', '10000人以上'],
    '薪资': ['面议', '10K以下', '10K-15K', '15K-20K', '20K-30K', '30K以上'],
    '公司领域': ['互联网', '电子商务', '游戏', '移动互联网', '计算机软件', 'IT服务', '人工智能',
                '数据服务', '信息安全', '云计算', '企业服务', '金融', '教育', '医疗健康',
                '文化娱乐', '生活服务', '广告传媒', '汽车', '硬件'],
    '福利待遇': ['五险一金', '补充医疗', '定期体检', '带薪年假', '餐补', '交通补助',
               '节日福利', '零食下午茶', '弹性工作', '周末双休', '年终奖', '免费班车',
               '包三餐', '员工旅游', '股票期权', '加班补助'],
}

# 不同岗位关键词对应的技能预设（选中/输入关键词后自动刷新技能需求下拉框）
KEYWORD_SKILL_PRESETS = {
    '游戏测试': ['功能测试', '性能测试', '黑盒测试', '白盒测试', '回归测试', '压力测试',
               '兼容性测试', 'Unity', 'Cocos', 'U3D', 'Python', 'SQL', 'Linux'],
    '软件测试': ['功能测试', '自动化测试', '接口测试', '性能测试', '回归测试',
               'Selenium', 'Appium', 'Postman', 'JMeter', 'Python', 'Java', 'SQL', 'Linux'],
    'Python': ['Python', 'Django', 'Flask', '爬虫', '数据分析', '机器学习',
              'MySQL', 'Redis', 'Linux', 'Docker', 'Git'],
    'Java': ['Java', 'Spring', 'SpringBoot', 'MySQL', 'Redis', 'RabbitMQ',
            'Linux', 'Maven', 'Git', '分布式'],
    '前端开发': ['JavaScript', 'TypeScript', 'Vue', 'React', 'HTML', 'CSS',
               'Webpack', 'Node.js', '小程序'],
    '产品经理': ['需求分析', '产品设计', 'Axure', '项目管理', '数据分析', '用户研究', 'PRD'],
    '数据分析': ['SQL', 'Python', 'Excel', 'Tableau', 'PowerBI', '数据可视化', '统计', '机器学习'],
    '运营': ['内容运营', '用户运营', '活动运营', '数据分析', '社群运营', '新媒体'],
    'UI设计': ['UI', 'UX', 'Figma', 'Sketch', 'Photoshop', 'Illustrator', '交互设计', '视觉设计'],
    '算法工程师': ['机器学习', '深度学习', 'Python', 'C++', 'PyTorch', 'TensorFlow', '自然语言处理', '推荐算法'],
    # 常见蓝领/生活服务岗位
    '水电': ['电工证', '电路安装', '水管安装', '管道维修', '电气维修', '线路检修', '施工现场', '图纸阅读'],
    '水电工': ['电工证', '电路安装', '水管安装', '管道维修', '电气维修', '线路检修', '施工现场', '图纸阅读'],
    '电工': ['电工证', '电路安装', '线路检修', '电气维修', '配电柜', 'PLC', '低压电工', '高压电工'],
    '焊工': ['焊工证', '电焊', '氩弧焊', '二保焊', '气割', '焊接工艺', '钢结构', '图纸阅读'],
    '木工': ['木工手艺', '家具制作', '装修木工', '门窗安装', '橱柜安装', '图纸阅读'],
    '司机': ['C1驾照', 'C2驾照', 'B2驾照', '驾驶经验', '熟悉路况', '无重大事故'],
    '厨师': ['厨师证', '中式烹饪', '西式烹饪', '面点', '菜品研发', '食品安全', '后厨管理'],
    '保洁': ['保洁经验', '清洁工具', '家政服务', '开荒保洁', '日常保洁'],
    '保安': ['保安证', '安全防范', '巡逻', '消防知识', '秩序维护', '退伍军人'],
    '销售': ['客户开发', '谈判技巧', '渠道拓展', '客户维护', '销售管理', '陌拜'],
    '客服': ['在线客服', '电话客服', '客户投诉处理', '沟通能力', '打字速度快'],
    '会计': ['会计证', '做账', '报税', '财务报表', '成本核算', '金蝶', '用友', 'Excel'],
    '文员': ['office办公', '文档管理', '资料整理', '会议记录', '考勤管理', 'Excel'],
    '行政': ['行政后勤', '办公用品管理', '活动组织', '考勤管理', '文件管理', '接待'],
    '人事': ['招聘', '员工关系', '薪酬核算', '绩效管理', '社保公积金', '劳动合同'],
    '仓管': ['仓库管理', '出入库', '盘点', 'ERP', '叉车证', '库存管理'],
}
# 无预设关键词时显示的通用技能（区别于具体岗位，保证有内容可选）
DEFAULT_SKILL_PRESETS = ['office办公', 'Excel', '沟通协调', '团队合作', '执行力', '责任心', '抗压能力', '时间管理']


def salary_bucket(salary):
    """把薪资数值归入区间档位，用于筛选下拉框"""
    if not salary:
        return '面议'
    k = salary / 1000
    if k < 10:
        return '10K以下'
    if k < 15:
        return '10K-15K'
    if k < 20:
        return '15K-20K'
    if k < 30:
        return '20K-30K'
    return '30K以上'


def job_matches_filters(job, filters, multi_selected=None):
    """判断岗位是否满足筛选条件（采集写CSV与界面列表共用，保证筛选规则一致）。
    filters: {字段名: 单选下拉选中的值}，值为 '全部' 或空表示该字段不过滤
    multi_selected: {字段名: 已勾选值集合}（公司领域/薪资/福利待遇等点击多选字段）
      - 福利待遇：岗位福利必须包含所有勾选项（满足全部才算通过）
      - 公司领域/薪资：岗位值命中任一勾选项即通过
    多个条件同时生效"""
    multi_selected = multi_selected or {}
    for field, sel in filters.items():
        if not sel or sel == '全部':
            continue
        if field == '薪资':
            if salary_bucket(job.get('薪资')) != sel:
                return False
        else:
            v = job.get(field)
            if isinstance(v, list):
                vals = {str(x) for x in v if x}
            else:
                vals = {str(v)} if v not in (None, '') else set()
            if sel not in vals:
                return False
    # 点击多选的字段
    for field, selected in multi_selected.items():
        if not selected:
            continue
        if field == '福利待遇':
            # 福利多选：岗位福利必须包含所有勾选的福利（满足全部才算通过）
            v = job.get('福利待遇')
            vals = {str(x) for x in v if x} if isinstance(v, list) else set()
            if not selected.issubset(vals):
                return False
        elif field == '薪资':
            # 薪资多选：命中任一档位即通过
            if salary_bucket(job.get('薪资')) not in selected:
                return False
        else:
            # 公司领域等多选：命中任一值即通过
            v = job.get(field)
            if isinstance(v, list):
                vals = {str(x) for x in v if x}
            else:
                vals = {str(v)} if v not in (None, '') else set()
            if not selected.intersection(vals):
                return False
    return True


def _html_to_text(s):
    """把职位描述的 HTML 片段转成纯文本（去标签、换行、反转义、去水印）"""
    if not s:
        return ''
    # 去掉 BOSS 直聘的防爬水印文字（常混在标题/正文中）
    s = re.sub(r'来自BOSS直聘', '', s)
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'</p>', '\n', s, flags=re.I)
    s = re.sub(r'<li[^>]*>', '\n· ', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    s = s.replace('\xa0', ' ')
    lines = [ln.strip() for ln in s.split('\n')]
    return '\n'.join(ln for ln in lines if ln).strip()


# 职位描述 DOM 候选选择器（按优先级排列，覆盖 PC 旧版与 geek 新版页面）
DESC_SELECTORS = [
    'css:.job-sec-text',
    'css:.job-detail-section .job-sec-text',
    'css:.job-detail-three .job-sec-text',
    'css:.job-detail .job-sec-text',
    'css:.job-detail-section',
    'css:.job-detail__content',
    'css:.description__content',
    'css:.job-detail__desc',
]


def fetch_job_description(page, job_id, timeout=DESC_FETCH_TIMEOUT, logger=None):
    """在新标签页打开岗位详情页抓取职位描述。
    直接通过 DOM 解析，避免监听接口路径未必匹配的问题。"""
    if not job_id:
        if logger:
            logger('fetch_job_description: job_id 为空，跳过')
        return ''
    tab = None
    try:
        tab = page.new_tab()
        tab.get(f'https://www.zhipin.com/job_detail/{job_id}.html')
        try:
            tab.wait.load_complete(timeout=timeout)
        except Exception:
            pass
        if logger:
            logger(f'fetch_job_description: 详情页加载完成，当前 URL = {tab.url}')

        desc = ''
        # DOM 多选择器提取（覆盖 PC 旧版、geek 新版等不同页面结构）
        for sel in DESC_SELECTORS:
            try:
                el = tab.ele(sel)
                text = el.text if el else ''
                if text and text.strip():
                    desc = text
                    if logger:
                        logger(f'fetch_job_description: 已从选择器 {sel} 提取到描述')
                    break
            except Exception:
                continue

        # 整页文本截取兜底：找「职位描述」之后的文本块
        if not desc:
            try:
                body_el = tab.ele('tag:body')
                t = body_el.text if body_el else ''
                m = re.search(r'职位描述\s*([\s\S]*?)(?=\s*(工作地址|任职要求|公司介绍|发布于|举报|相关职位|\n\n参考))', t)
                if m and m.group(1).strip():
                    desc = m.group(1).strip()
                    if logger:
                        logger('fetch_job_description: 已从整页文本截取到描述')
            except Exception:
                pass

        if logger:
            logger(f'fetch_job_description: 最终描述长度 = {len(desc) if desc else 0}')
        if not desc and logger:
            # 诊断：描述为空时，输出页面文本长度及是否含关键词，用于判断是页面未渲染还是选择器不对
            try:
                body_el = tab.ele('tag:body')
                t = body_el.text if body_el else ''
                logger(f'fetch_job_description: [诊断] body文本长度={len(t)}，含「职位描述」={"职位描述" in t}，'
                       f'含「任职要求」={"任职要求" in t}，含「岗位职责」={"岗位职责" in t}')
            except Exception:
                logger('fetch_job_description: [诊断] 无法读取 body 文本')
        return _html_to_text(desc)
    except Exception as e:
        if logger:
            logger(f'fetch_job_description: 异常 {type(e).__name__}: {e}')
        return ''
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:
                pass


def crawl_boss_zhipin(city_name='北京', city_code=None, keyword='游戏测试', total_pages=5,
                      on_log=None, on_job=None, province_map=None, should_stop=None,
                      browser='chrome'):
    """采集BOSS直聘岗位数据。
    city_code: BOSS直聘城市代码（可为空，为空时从热门城市表自动查找）
    province_map: 城市名->省份 映射（用于补全省份信息，可为空）
    on_log: 日志回调(接收字符串)
    on_job: 每采集到一条岗位的回调(接收dict)
    should_stop: 停止回调(返回 True 时中止采集，可为空)
    说明：本函数只负责采集，不过滤数据；筛选统一在界面层完成，避免采集与界面筛选基准不一致。
    返回最终保存的CSV文件路径
    """
    def say(msg):
        if on_log:
            on_log(msg)
        else:
            log(msg)

    if not city_code:
        city_code = CITY_OPTIONS.get(city_name, '101010100')
    total_pages = max(1, int(total_pages))

    # 1. 初始化CSV文件（若被Excel占用会自动换新文件名）
    today = datetime.date.today().strftime('%Y%m%d')
    csv_name = f'boss_{city_name}_{keyword}_{today}.csv'
    f, csv_name = open_csv_retry(csv_name)
    with f:
        csv_writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        csv_writer.writeheader()

        # 2. 连接调试模式浏览器（Chrome/Edge 需先以调试模式启动）
        browser_name = BROWSERS.get(browser, BROWSERS['chrome'])['name']
        say(f'正在连接调试模式 {browser_name}（{CHROME_DEBUG_ADDR}）...')
        co = ChromiumOptions()
        co.debugger_address = CHROME_DEBUG_ADDR
        dp = ChromiumPage(co)

        # 监听岗位列表接口
        dp.listen.start('joblist')
        target_url = f"https://www.zhipin.com/web/geek/jobs?query={quote(keyword)}&city={city_code}"
        say(f'正在打开页面：{city_name} · {keyword}')
        dp.get(target_url)

        # 3. 循环翻页采集
        seen_job_ids = set()          # 去重：记录已写入的岗位 jobId
        total_written = 0
        stopped = False
        for page in range(1, total_pages + 1):
            if should_stop and should_stop():
                say('⚠ 收到停止指令，采集已中止')
                stopped = True
                break
            say(f'========== 正在采集第{page}页数据内容 ==========')
            try:
                resp = dp.listen.wait(timeout=JOBLIST_TIMEOUT)
                if resp is None or not hasattr(resp, 'response'):
                    say(f'第{page}页：未监听到岗位列表接口（可能页面未加载、接口路径变更或触发风控），跳过本页')
                    continue
                # 防御性取值：部分情况下 resp.response 可能是布尔值而非响应对象
                response = resp.response
                resp_body = getattr(response, 'body', None) if response is not None else None
                if not resp_body:
                    say(f'第{page}页：接口已捕获但响应体为空，跳过本页')
                    continue

                # 解析接口返回的 JSON（逐层判空，避免 KeyError 导致整页数据丢失）
                json_data = resp_body
                if not isinstance(json_data, dict):
                    say(f'第{page}页：接口响应不是 JSON 对象，实际类型：{type(json_data).__name__}')
                    continue
                # 检查接口业务码：0 为正常；非 0 多为风控/未登录/接口变更
                api_code = json_data.get('code')
                if api_code not in (None, 0):
                    say(f'第{page}页：接口返回业务码 code={api_code}（msg={json_data.get("message", "")}），'
                        f'多为风控拦截、未登录或接口变更，建议检查登录状态后重试，跳过本页')
                    continue
                zp_data = json_data.get('zpData')
                if not zp_data:
                    say(f'第{page}页：响应中缺少 zpData 字段')
                    continue
                job_list = zp_data.get('jobList')
                if not job_list:
                    say(f'第{page}页：zpData 中没有岗位列表 jobList（可能被风控或页面改版）')
                    continue

                page_new = 0
                desc_count = 0
                for job in job_list:
                    if should_stop and should_stop():
                        say('⚠ 收到停止指令，本页后续岗位已跳过')
                        break
                    # 去重：同一岗位翻页重复出现时只保留第一条（jobId 缺失时不过滤，避免误删）
                    job_id = job.get('jobId') or job.get('encryptJobId') or ''
                    if job_id:
                        if job_id in seen_job_ids:
                            continue
                        seen_job_ids.add(job_id)
                    city_name_job = job.get('cityName', '')
                    gps = job.get('gps') or {}   # gps 可能为 null，防御性取值
                    salary_desc = job.get('salaryDesc', '')
                    # 采集时同步抓取职位描述（新标签页打开详情页，不打断列表页）
                    # 限制每页抓取条数并加间隔，降低频率、减少触发风控的概率
                    if desc_count < DESC_FETCH_BATCH:
                        say(f'正在抓取岗位描述：{job.get("jobName", "")}（{job.get("brandName", "")}）')
                        job_desc = fetch_job_description(dp, job_id, logger=say)
                        desc_count += 1
                        time.sleep(DESC_FETCH_DELAY)
                    else:
                        job_desc = ''
                        say(f'本页职位描述已达抓取上限（{DESC_FETCH_BATCH} 条），后续岗位描述留空')
                    if not job_desc:
                        say('  （未获取到职位描述，已留空）')
                    job_info = {
                        '岗位名称': job.get('jobName', ''),
                        '公司': job.get('brandName', ''),
                        '规模': job.get('brandScaleName', ''),
                        '公司领域': job.get('brandIndustry', ''),
                        '学历要求': job.get('jobDegree', ''),
                        '经验要求': job.get('jobExperience', ''),
                        '技能需求': job.get('skills', []),
                        '福利待遇': job.get('welfareList', []),
                        '薪资': parse_month_salary(salary_desc),
                        '薪资原文': salary_desc,
                        '省': (province_map or {}).get(city_name_job, ''),
                        '市': city_name_job,
                        '区': job.get('areaDistrict', ''),
                        '商圈': job.get('businessDistrict', ''),
                        '经度': gps.get('longitude', ''),
                        '纬度': gps.get('latitude', ''),
                        '岗位链接': f'https://www.zhipin.com/job_detail/{job_id}.html' if job_id else '',
                        'jobId': job_id,
                        '职位描述': job_desc,
                    }
                    # 写入前做公式注入防护（= + - @ 开头加 '），并清洗描述文本（去水印、规范空行）
                    clean_job_info = {k: sanitize_csv_value(v) for k, v in job_info.items()}
                    if clean_job_info.get('职位描述'):
                        raw_desc = clean_job_info['职位描述']
                        raw_desc = re.sub(r'来自BOSS直聘', '', raw_desc)
                        raw_desc = re.sub(r'\n{3,}', '\n\n', raw_desc).strip()
                        clean_job_info['职位描述'] = raw_desc
                    csv_writer.writerow(clean_job_info)
                    page_new += 1
                    total_written += 1
                    if on_job:
                        on_job(job_info)
                dup_skipped = len(job_list) - page_new
                if dup_skipped > 0:
                    say(f'第{page}页：接口返回 {len(job_list)} 条，其中 {dup_skipped} 条重复已跳过，实际写入 {page_new} 条')
                else:
                    say(f'第{page}页：成功获取 {len(job_list)} 条岗位数据，开始写入CSV...')
                # 下滑触发下一页加载
                dp.scroll.to_bottom()
            except Exception as e:
                say(f'第{page}页数据采集异常：{type(e).__name__}: {e}')
                say(traceback.format_exc())
                continue
            time.sleep(PAGE_SLEEP_SECONDS)
        if stopped:
            say(f'========== 采集已停止（去重后共写入 {total_written} 条），结果已存入 {csv_name} ==========')
        else:
            say(f'========== 全部{total_pages}页数据采集完成（去重后共写入 {total_written} 条），结果已存入 {csv_name} ==========')
    return csv_name


# 51job 职位描述 DOM 候选选择器（覆盖旧版与新版详情页）
DESC_51JOB_SELECTORS = [
    'css:.job_msg',
    'css:.bmsg.job_msg.inbox',
    'css:.des',
    'css:.job-description',
    'css:.job-detail__content',
    'css:div.job-detail',
]


def fetch_51job_description(page, job_href, timeout=DESC_FETCH_TIMEOUT, logger=None):
    """在新标签页打开前程无忧（51job）岗位详情页抓取职位描述。
    直接通过 DOM 解析，失败返回空字符串，不影响主流程。"""
    if not job_href:
        if logger:
            logger('fetch_51job_description: job_href 为空，跳过')
        return ''
    tab = None
    try:
        tab = page.new_tab()
        tab.get(job_href)
        try:
            tab.wait.load_complete(timeout=timeout)
        except Exception:
            pass
        if logger:
            logger(f'fetch_51job_description: 详情页加载完成，当前 URL = {tab.url}')

        desc = ''
        for sel in DESC_51JOB_SELECTORS:
            try:
                el = tab.ele(sel)
                text = el.text if el else ''
                if text and text.strip():
                    desc = text
                    if logger:
                        logger(f'fetch_51job_description: 已从选择器 {sel} 提取到描述')
                    break
            except Exception:
                continue

        # 整页文本截取兜底：找「职位描述/岗位职责/任职要求」之后的文本块
        if not desc:
            try:
                body_el = tab.ele('tag:body')
                t = body_el.text if body_el else ''
                for lead in ['职位描述', '岗位职责', '任职要求']:
                    m = re.search(re.escape(lead) + r'\s*([\s\S]*?)(?=\s*(工作地址|公司信息|公司简介|发布于|举报|\n\n参考))', t)
                    if m and m.group(1).strip():
                        desc = m.group(1).strip()
                        if logger:
                            logger(f'fetch_51job_description: 已从整页文本截取到描述（标记「{lead}」）')
                        break
            except Exception:
                pass

        if logger:
            logger(f'fetch_51job_description: 最终描述长度 = {len(desc) if desc else 0}')
        return _html_to_text(desc)
    except Exception as e:
        if logger:
            logger(f'fetch_51job_description: 异常 {type(e).__name__}: {e}')
        return ''
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:
                pass


def crawl_51job(city_name='北京', city_code=None, keyword='游戏测试', total_pages=5,
                on_log=None, on_job=None, should_stop=None, browser='chrome'):
    """采集前程无忧（51job）岗位数据。
    city_code: 51job 城市代码（jobArea 参数，为空时从 CITY_51JOB_OPTIONS 自动查找）
    on_log: 日志回调(接收字符串)
    on_job: 每采集到一条岗位的回调(接收dict)
    should_stop: 停止回调(返回 True 时中止采集，可为空)
    字段说明：51job 与 BOSS 直聘字段体系不同，这里统一映射到 CSV_FIELDNAMES 的字段名，
    薪资用 parse_51job_salary 解析（年薪折算为月薪），保证与 BOSS 采集结果可用同一套筛选逻辑。
    返回最终保存的CSV文件路径
    """
    def say(msg):
        if on_log:
            on_log(msg)
        else:
            log(msg)

    if not city_code:
        city_code = CITY_51JOB_OPTIONS.get(city_name, CITY_51JOB_OPTIONS.get('北京', '010000'))
    total_pages = max(1, int(total_pages))

    # 1. 初始化CSV文件（若被Excel占用会自动换新文件名）
    today = datetime.date.today().strftime('%Y%m%d')
    csv_name = f'51job_{city_name}_{keyword}_{today}.csv'
    f, csv_name = open_csv_retry(csv_name)
    with f:
        csv_writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        csv_writer.writeheader()

        # 2. 连接调试模式浏览器（Chrome/Edge 需先以调试模式启动）
        browser_name = BROWSERS.get(browser, BROWSERS['chrome'])['name']
        say(f'正在连接调试模式 {browser_name}（{CHROME_DEBUG_ADDR}）...')
        co = ChromiumOptions()
        co.debugger_address = CHROME_DEBUG_ADDR
        dp = ChromiumPage(co)

        # 监听 51job 搜索接口（api/job/search-pc）
        dp.listen.start('search-pc')
        target_url = f"https://we.51job.com/pc/search?jobArea={quote(city_code)}&keyword={quote(keyword)}"
        say(f'正在打开页面：{city_name} · {keyword}')
        dp.get(target_url)

        # 3. 循环翻页采集
        seen_job_ids = set()          # 去重：记录已写入的岗位 jobId
        total_written = 0
        stopped = False
        for page in range(1, total_pages + 1):
            if should_stop and should_stop():
                say('⚠ 收到停止指令，采集已中止')
                stopped = True
                break
            say(f'========== 正在采集第{page}页数据内容 ==========')
            try:
                # 第2页起：滚动到底部加载「下一页」按钮，点击触发下一次搜索接口
                if page > 1:
                    dp.scroll.to_bottom()
                    next_btn = dp.ele('css:button.btn-next', timeout=JOBLIST_TIMEOUT)
                    if not next_btn:
                        say(f'第{page}页：未找到「下一页」按钮（可能已到最后一页），停止翻页')
                        break
                    next_btn.click()

                resp = dp.listen.wait(timeout=JOBLIST_TIMEOUT)
                if resp is None or not hasattr(resp, 'response'):
                    say(f'第{page}页：未监听到搜索接口（可能页面未加载、接口路径变更或触发风控），跳过本页')
                    continue
                # 防御性取值：部分情况下 resp.response 可能是布尔值而非响应对象
                response = resp.response
                resp_body = getattr(response, 'body', None) if response is not None else None
                # 51job 接口可能把 body 以字符串返回，需先转 JSON 对象
                if isinstance(resp_body, str):
                    try:
                        resp_body = json.loads(resp_body)
                    except Exception:
                        resp_body = None
                if not resp_body:
                    say(f'第{page}页：接口已捕获但响应体为空，跳过本页')
                    continue
                if not isinstance(resp_body, dict):
                    say(f'第{page}页：接口响应不是 JSON 对象，实际类型：{type(resp_body).__name__}')
                    continue

                # 解析接口返回数据（逐层判空，避免 KeyError 导致整页丢失）
                result_job = resp_body.get('resultbody', {}).get('job')
                if not result_job:
                    say(f'第{page}页：响应中缺少 resultbody.job 字段')
                    continue
                job_list = result_job.get('items')
                if not job_list:
                    say(f'第{page}页：没有岗位列表 items（可能被风控或页面改版）')
                    continue

                page_new = 0
                desc_count = 0
                for item in job_list:
                    if should_stop and should_stop():
                        say('⚠ 收到停止指令，本页后续岗位已跳过')
                        break
                    # 去重：同一岗位翻页重复出现时只保留第一条（jobId 缺失时不过滤，避免误删）
                    job_id = str(item.get('jobId') or '')
                    if job_id:
                        if job_id in seen_job_ids:
                            continue
                        seen_job_ids.add(job_id)
                    # 地区为嵌套结构，可能为 null，防御性取值
                    area = item.get('jobAreaLevelDetail') or {}
                    salary_desc = item.get('provideSalaryString', '')
                    # 技能标签：51job 的 jobTags 前2个通常是插入广告/无关标签，跳过
                    tags = item.get('jobTags') or []
                    skills = [str(t) for t in tags[2:] if t]
                    # 福利待遇：51job 列表接口字段名不固定，做多字段兜底
                    welfare = item.get('jobWelfare') or item.get('jobwelf') or item.get('welfare') or []
                    if isinstance(welfare, dict):
                        welfare = [str(v) for k, v in welfare.items() if v]
                    elif isinstance(welfare, str):
                        welfare = [welfare]
                    elif not isinstance(welfare, list):
                        welfare = []
                    # 详情页链接可能为相对路径，补全为绝对地址
                    job_href = item.get('jobHref') or ''
                    if job_href and not str(job_href).startswith('http'):
                        job_href = 'https://jobs.51job.com' + (job_href if str(job_href).startswith('/') else '/' + str(job_href))
                    # 采集时同步抓取职位描述（新标签页打开详情页，不打断列表页）
                    if desc_count < DESC_FETCH_BATCH:
                        say(f'正在抓取岗位描述：{item.get("jobName", "")}（{item.get("fullCompanyName", "")}）')
                        job_desc = fetch_51job_description(dp, job_href, logger=say)
                        desc_count += 1
                        time.sleep(DESC_FETCH_DELAY)
                    else:
                        job_desc = ''
                        say(f'本页职位描述已达抓取上限（{DESC_FETCH_BATCH} 条），后续岗位描述留空')
                    if not job_desc:
                        say('  （未获取到职位描述，已留空）')
                    job_info = {
                        '岗位名称': item.get('jobName', ''),
                        '公司': item.get('fullCompanyName', '') or item.get('companyName', ''),
                        '规模': item.get('companySizeString', ''),
                        '公司领域': item.get('industryType1Str', ''),
                        '学历要求': item.get('degreeString', ''),
                        '经验要求': item.get('workYearString', ''),
                        '技能需求': skills,
                        '福利待遇': welfare,
                        '薪资': parse_51job_salary(salary_desc),
                        '薪资原文': salary_desc,
                        '省': area.get('provinceString', ''),
                        '市': area.get('cityString', '') or city_name,
                        '区': area.get('districtString', ''),
                        '商圈': item.get('landmarkString', ''),
                        '经度': item.get('lon', ''),
                        '纬度': item.get('lat', ''),
                        '岗位链接': job_href,
                        'jobId': job_id,
                        '职位描述': job_desc,
                    }
                    # 写入前做公式注入防护（= + - @ 开头加 '）
                    clean_job_info = {k: sanitize_csv_value(v) for k, v in job_info.items()}
                    csv_writer.writerow(clean_job_info)
                    page_new += 1
                    total_written += 1
                    if on_job:
                        on_job(job_info)
                dup_skipped = len(job_list) - page_new
                if dup_skipped > 0:
                    say(f'第{page}页：接口返回 {len(job_list)} 条，其中 {dup_skipped} 条重复已跳过，实际写入 {page_new} 条')
                else:
                    say(f'第{page}页：成功获取 {len(job_list)} 条岗位数据，开始写入CSV...')
            except Exception as e:
                say(f'第{page}页数据采集异常：{type(e).__name__}: {e}')
                say(traceback.format_exc())
                continue
            time.sleep(PAGE_SLEEP_SECONDS)
        if stopped:
            say(f'========== 采集已停止（去重后共写入 {total_written} 条），结果已存入 {csv_name} ==========')
        else:
            say(f'========== 全部{total_pages}页数据采集完成（去重后共写入 {total_written} 条），结果已存入 {csv_name} ==========')
    return csv_name


# ==================== 图形界面 ====================

class MultiSelectDropdown(ctk.CTkButton):
    """内联多选下拉框：点击就地展开选项，逐项点选即可多选（勾选立即生效）。
    不弹单独窗口、没有"确定"按钮；点按钮收起，点窗口其他区域自动收起。"""

    POPUP_HEIGHT = MULTI_POPUP_HEIGHT

    def __init__(self, master, options, selected, on_change, width=110, height=28, font=None):
        super().__init__(master, text='全部  ▼', width=width, height=height, font=font,
                         command=self._toggle)
        self.selected = selected          # 外部共享的已选集合（外部直接增删）
        self.on_change = on_change        # 勾选变化时回调（无参数）
        self.options = list(options)
        self._font = font
        self._popup = None
        self._box = None
        self._checkboxes = {}
        self._root_bind_id = None

    def refresh_text(self):
        """按共享已选集合刷新按钮文字"""
        n = len(self.selected)
        self.configure(text=('全部' if not n else f'已选{n}项') + '  ▼')

    def set_options(self, options):
        """更新可选列表（保留已选项）"""
        self.options = list(options)
        if self._popup is not None:
            self._refresh_items()

    def _toggle(self):
        if self._popup is not None:
            self._close_popup()
        else:
            self._open_popup()

    def _open_popup(self):
        # 先收起自己可能残留的面板，避免重复绑定点击事件
        self._close_popup()
        root = self.winfo_toplevel()
        # 宽度：按最长选项文字自适应，避免文字被截断（含复选框方块 / 滚动条 / 内边距）
        try:
            max_text = max((self._font.measure(str(o)) for o in self.options), default=0)
        except Exception:
            max_text = 0
        popup_w = max(self._current_width, int(max_text) + 72)
        # 高度：贴合条目数自适应，避免底部留白；超过上限才出现滚动条
        item_h = 30
        popup_h = min(self.POPUP_HEIGHT, max(40, len(self.options) * item_h + 12))
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        popup = ctk.CTkToplevel(root)
        popup.overrideredirect(True)
        popup.attributes('-topmost', True)
        popup.geometry(f'{popup_w}x{popup_h}+{x}+{y}')
        box = ctk.CTkScrollableFrame(popup, width=popup_w, height=popup_h)
        box.pack(fill='both', expand=True)
        self._popup = popup
        self._box = box
        self._refresh_items()
        # 点击主窗口其他区域时收起
        self._root_bind_id = root.bind('<Button-1>', self._on_any_click, add='+')

    def _refresh_items(self):
        for cb in self._checkboxes.values():
            try:
                cb.destroy()
            except Exception:
                pass  # 面板已销毁时旧控件无法再销毁，忽略即可
        self._checkboxes = {}
        for opt in self.options:
            var = ctk.BooleanVar(value=opt in self.selected)
            cb = ctk.CTkCheckBox(self._box, text=str(opt), variable=var,
                                 font=self._font, checkbox_width=18, checkbox_height=18,
                                 command=lambda o=opt: self._on_item(o))
            cb.pack(anchor='w', fill='x', pady=3, padx=8)
            self._checkboxes[opt] = cb

    def _on_item(self, opt):
        cb = self._checkboxes.get(opt)
        if cb is None:
            return
        if cb.get():
            self.selected.add(opt)
        else:
            self.selected.discard(opt)
        self.refresh_text()
        self.on_change()

    def _widget_is_or_descends_from(self, widget, target):
        """判断 widget 是否是 target 自身或其后代（沿 master 链上溯，比字符串比较可靠）"""
        cur = widget
        while cur is not None:
            if cur is target:
                return True
            try:
                cur = cur.master
            except Exception:
                return False
        return False

    def _on_any_click(self, event):
        if self._popup is None:
            return
        try:
            w = event.widget
            # 点在自己按钮上：不在这里收起，交给按钮的 _toggle 处理
            if w is not None and self._widget_is_or_descends_from(w, self):
                return
        except Exception:
            pass
        self._close_popup()

    def _close_popup(self):
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None
            self._box = None
            self._checkboxes = {}   # 面板已销毁，清空旧勾选项引用，避免下次打开时误销毁
        root = self.winfo_toplevel()
        if self._root_bind_id is not None:
            try:
                root.unbind('<Button-1>', self._root_bind_id)
            except Exception:
                pass
            self._root_bind_id = None


class BossGuiApp(ctk.CTk):
    """BOSS直聘岗位采集助手 - 图形界面"""

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode('light')
        ctk.set_default_color_theme('blue')

        self.title('岗位采集助手')

        # ==================== 多分辨率 / 多缩放自适应 ====================
        # 界面按 1300x1294 逻辑像素设计（在 4K 屏幕上完整显示）。若当前屏幕
        # 可用区放不下整个设计尺寸，就整体等比缩小控件与窗口，保证在不同分辨率、
        # 不同 Windows 缩放比下都能完整显示，而不是被窗口边缘裁掉。
        self._dpi_scale = self._get_dpi_scale()          # 系统 DPI 缩放（100%=1.0，150%=1.5）
        design_w, design_h = 1300, 1294                  # 设计基准尺寸（96DPI 逻辑像素）
        avail_w, avail_h = self._get_screen_avail()      # 屏幕可用区（已换算为逻辑像素）
        fit = min(avail_w / design_w, avail_h / design_h, 1.0)  # 只缩小、不放大
        fit = max(fit, 0.4)                              # 下限，与 customtkinter 的缩放下限一致
        self._fit_scale = fit
        ctk.set_widget_scaling(fit)                      # 等比缩放所有控件尺寸/字号
        ctk.set_window_scaling(fit)                      # 等比缩放窗口，保持窗口与控件缩放一致
        self.geometry(f'{design_w}x{design_h}')
        # 允许用户放大（小屏上也更自由）；最小尺寸锁定为自适应后的完整尺寸，
        # 避免被拖小后裁掉内容。
        self.minsize(design_w, design_h)
        self.resizable(True, True)

        self.msg_queue = queue.Queue()   # 后台线程 -> 界面 的消息队列
        self.jobs = []                   # 已采集的岗位数据列表（全部）
        self.filtered_jobs = []          # 筛选后可见的岗位列表
        self.row_buttons = []            # 列表区每一行的按钮控件
        self.selected_idx = None         # 当前选中的行号（对应 filtered_jobs）
        self.crawling = False            # 是否正在采集
        self._stop_event = threading.Event()  # 停止采集事件（线程安全）
        self._empty_hint = None          # 列表为空时的提示文字
        self.all_cities = dict(CITY_OPTIONS)   # 全国城市表 {城市名: 城市代码}
        self.filter_vars = {}            # 筛选下拉框变量 {字段: StringVar}
        self.filter_menus = {}           # 筛选下拉框控件 {字段: CTkOptionMenu}
        self.location_vars = {}          # 省市区筛选变量 {字段: StringVar}
        self.location_menus = {}         # 省市区筛选控件 {字段: CTkOptionMenu}
        self.location_items = {}         # 省市区筛选所在容器（用于显示/隐藏）
        self._location_shown = False     # 省市区筛选是否已显示（采集到数据后显示）
        self.multi_selected = {f: set() for f in MULTI_SELECT_FIELDS}  # 点击多选字段的已选项（空集合=不过滤）
        self.multi_options = {f: [] for f in MULTI_SELECT_FIELDS}      # 各多选字段的可选列表（预设+实际数据）
        self.multi_btns = {}                                           # 多选按钮控件 {字段: CTkButton}
        self.province_map = {}           # 城市名->省份 映射（用于补全省份信息）
        self.current_site = 'boss'                        # 当前采集网站：'boss' 或 '51job'
        self.current_browser = detect_browser()           # 自动检测浏览器：优先 Edge，其次 Chrome
        self.all_51job_cities = dict(CITY_51JOB_OPTIONS)  # 51job 城市表 {城市名: 城市代码}
        self._theme_key = 'boss'                          # 当前主题：'boss' 或 '51job'
        self._theme = THEME_COLORS[self._theme_key]       # 当前主题色 {primary, hover}
        self._theme_widgets = []                          # 需跟随主题变色的控件（在 _build_ui 中填充）
        # 加载保存的设置（导出目录等），默认使用脚本所在目录
        settings = load_settings()
        self.export_dir = settings.get('export_dir') or BASE_DIR

        # 确保日志目录存在（日志按日期滚动，保留历史，不再覆盖）
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except Exception:
            pass

        self._build_ui()
        self.after(POLL_INTERVAL_MS, self._poll_queue)
        # 监听岗位关键词变化（下拉选择或输入框输入都会触发），自动刷新技能需求预设
        self.keyword_var.trace_add('write', lambda *a: self._schedule_keyword_refresh())
        self._load_cities_async()        # 后台加载全国城市，填充下拉框
        self._refresh_keyword_presets()  # 按默认岗位关键词刷新技能需求预设

    # ---------- 屏幕自适应 ----------

    def _get_dpi_scale(self):
        """返回系统 DPI 缩放比例（100% -> 1.0，150% -> 1.5，200% -> 2.0）。"""
        try:
            return ctk.ScalingTracker.get_window_dpi_scaling(self)
        except Exception:
            pass
        try:
            # 兜底：用 Tk 的 DPI 换算（96DPI 为 1.0）
            return self.winfo_fpixels('1i') / 96.0
        except Exception:
            return 1.0

    def _get_screen_avail(self):
        """返回屏幕可用区换算成 96DPI 逻辑像素后的 (宽, 高)。

        取 Windows 工作区（SPI_GETWORKAREA，物理像素，已排除任务栏），除以 DPI
        缩放得到逻辑像素，再扣除标题栏/边框高度，保证窗口外框能完整放入屏幕。
        失败时退回整屏尺寸（GetSystemMetrics，物理像素）并预留任务栏/标题栏余量。"""
        titlebar_h = 30  # 标题栏 + 边框高度（逻辑像素）
        try:
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                            ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

            rect = RECT()
            # SPI_GETWORKAREA = 0x0030，返回主屏工作区（物理像素，排除任务栏）
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            w = (rect.right - rect.left) / self._dpi_scale
            h = (rect.bottom - rect.top) / self._dpi_scale - titlebar_h
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
        try:
            import ctypes
            # SM_CXSCREEN=0 / SM_CYSCREEN=1：主屏物理分辨率
            w = ctypes.windll.user32.GetSystemMetrics(0) / self._dpi_scale - 20
            h = ctypes.windll.user32.GetSystemMetrics(1) / self._dpi_scale - 100
            return w, h
        except Exception:
            return 1200, 720

    # ---------- 界面搭建 ----------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # 顶部标题
        self.title_label = ctk.CTkLabel(
            self, text='岗位采集助手',
            font=ctk.CTkFont(size=28, weight='bold'), text_color=self._theme['primary']
        )
        self.title_label.grid(row=0, column=0, pady=(18, 2))
        ctk.CTkLabel(
            self, text='支持 BOSS直聘 / 前程无忧(51job) · 自动采集 · 点选详情 · 一键导出表格',
            font=ctk.CTkFont(size=14), text_color='gray'
        ).grid(row=1, column=0, pady=(0, 6))

        # ---------- 参数区 ----------
        param_frame = ctk.CTkFrame(self, corner_radius=12)
        param_frame.grid(row=2, column=0, padx=20, pady=10, sticky='ew')

        # 采集网站切换（第一行）
        ctk.CTkLabel(param_frame, text='采集网站：', font=ctk.CTkFont(size=15)).grid(row=0, column=0, padx=(16, 4), pady=(12, 4))
        self.site_var = ctk.StringVar(value='BOSS直聘')
        self.site_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.site_var, values=['BOSS直聘', '前程无忧(51job)'],
            width=160, font=ctk.CTkFont(size=14), command=self._on_site_selected
        )
        self.site_menu.grid(row=0, column=1, padx=(0, 14), pady=(12, 4), sticky='w')
        _fit_optionmenu(self.site_menu)

        # 打开登录页：在调试浏览器里直接打开当前网站的登录页，方便首次登录
        self.login_btn = ctk.CTkButton(
            param_frame, text='打开登录页', font=ctk.CTkFont(size=14),
            width=110, height=32, command=self._open_login_page
        )
        self.login_btn.grid(row=0, column=2, padx=(0, 8), pady=(12, 4))

        # 使用说明：弹窗展示一步一步的操作步骤
        self.help_btn = ctk.CTkButton(
            param_frame, text='使用说明', font=ctk.CTkFont(size=14),
            width=110, height=32, command=self._show_help,
            fg_color='transparent', border_width=2, border_color='#2e8b57',
            text_color='#2e8b57', hover_color='#e6f4ec'
        )
        self.help_btn.grid(row=0, column=3, padx=(0, 14), pady=(12, 4))

        ctk.CTkLabel(param_frame, text='选择城市：', font=ctk.CTkFont(size=15)).grid(row=1, column=0, padx=(16, 4), pady=(4, 14))
        self.city_var = ctk.StringVar(value='北京')
        self.city_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.city_var, values=list(CITY_OPTIONS.keys()),
            width=110, font=ctk.CTkFont(size=14), command=self._on_city_selected
        )
        self.city_menu.grid(row=1, column=1, padx=(0, 8), pady=(4, 14))
        _fit_optionmenu(self.city_menu)
        ctk.CTkEntry(
            param_frame, textvariable=self.city_var, width=110,
            placeholder_text='可自定义输入', font=ctk.CTkFont(size=14)
        ).grid(row=1, column=2, padx=(0, 14), pady=(4, 14))

        ctk.CTkLabel(param_frame, text='岗位关键词：', font=ctk.CTkFont(size=15)).grid(row=1, column=3, padx=(0, 4), pady=(4, 14))
        self.keyword_var = ctk.StringVar(value='游戏测试')
        self.keyword_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.keyword_var, values=KEYWORD_OPTIONS,
            width=130, font=ctk.CTkFont(size=14), command=self._on_keyword_selected
        )
        self.keyword_menu.grid(row=1, column=4, padx=(0, 8), pady=(4, 14))
        _fit_optionmenu(self.keyword_menu)
        self.keyword_entry = ctk.CTkEntry(
            param_frame, textvariable=self.keyword_var, width=130,
            placeholder_text='可自定义输入', font=ctk.CTkFont(size=14))
        self.keyword_entry.grid(row=1, column=5, padx=(0, 14), pady=(4, 14))

        ctk.CTkLabel(param_frame, text='采集页数：', font=ctk.CTkFont(size=15)).grid(row=1, column=6, padx=(0, 4), pady=(4, 14))
        self.pages_var = ctk.StringVar(value='5')
        ctk.CTkEntry(param_frame, textvariable=self.pages_var, width=60, font=ctk.CTkFont(size=14)).grid(row=1, column=7, pady=(4, 14))

        self.start_btn = ctk.CTkButton(
            param_frame, text='开始采集', font=ctk.CTkFont(size=16, weight='bold'),
            width=125, height=38, command=self._start_crawl, text_color='white'
        )
        self.start_btn.grid(row=1, column=8, padx=(4, 8), pady=(4, 14))
        self.stop_btn = ctk.CTkButton(
            param_frame, text='停止采集', font=ctk.CTkFont(size=16, weight='bold'),
            width=125, height=38, command=self._stop_crawl,
            fg_color='#e74c3c', hover_color='#c0392b', text_color='white'
        )
        self.stop_btn.grid(row=1, column=9, padx=(0, 20), pady=(4, 14))

        # ---------- 筛选区（第二行，对采集结果按字段筛选） ----------
        filter_frame = ctk.CTkFrame(self, corner_radius=12)
        filter_frame.grid(row=3, column=0, padx=20, pady=(0, 10), sticky='ew')
        ctk.CTkLabel(filter_frame, text='筛选条件（选择后列表自动更新）：', font=ctk.CTkFont(size=13, weight='bold')).grid(
            row=0, column=0, columnspan=7, padx=(16, 0), pady=(10, 2), sticky='w')
        # 7 个字段分两行（第一行5个，第二行2个）排布
        for i, field in enumerate(FILTER_FIELDS):
            r = 1 + i // 5
            c = i % 5
            item = ctk.CTkFrame(filter_frame, fg_color='transparent')
            item.grid(row=r, column=c, padx=10, pady=4, sticky='w')
            ctk.CTkLabel(item, text=field, font=ctk.CTkFont(size=12), width=64, anchor='w').grid(row=0, column=0)
            if field in MULTI_SELECT_FIELDS:
                # 公司领域/薪资/福利待遇支持多选：点下拉框就地展开，逐项勾选即可
                dropdown = MultiSelectDropdown(
                    item, options=PRESET_FILTER_OPTIONS.get(field, []),
                    selected=self.multi_selected[field],
                    on_change=self._apply_filter,
                    width=100, height=28, font=ctk.CTkFont(size=12))
                dropdown.grid(row=0, column=1)
                self.multi_btns[field] = dropdown
                continue
            var = ctk.StringVar(value='全部')
            init_values = ['全部'] + PRESET_FILTER_OPTIONS.get(field, [])
            menu = ctk.CTkOptionMenu(
                item, variable=var, values=init_values, width=100, height=28,
                font=ctk.CTkFont(size=12), command=self._apply_filter)
            menu.grid(row=0, column=1)
            self.filter_vars[field] = var
            self.filter_menus[field] = menu
            _fit_optionmenu(menu)
        # 重置按钮：橙色描边样式，与左侧下拉框明显区分
        ctk.CTkButton(
            filter_frame, text='重置筛选', width=90, height=30,
            font=ctk.CTkFont(size=13), command=self._reset_filters,
            fg_color='transparent', border_width=2, border_color='#e67e22',
            text_color='#e67e22', hover_color='#fde8d7'
        ).grid(row=2, column=5, padx=16, pady=4, sticky='n')

        # ---------- 第三行：省市区筛选（采集到数据后才显示） ----------
        for i, field in enumerate(LOCATION_FIELDS):
            item = ctk.CTkFrame(filter_frame, fg_color='transparent')
            item.grid(row=3, column=i, padx=10, pady=4, sticky='w')
            ctk.CTkLabel(item, text=field, font=ctk.CTkFont(size=12), width=64, anchor='w').grid(row=0, column=0)
            var = ctk.StringVar(value='全部')
            menu = ctk.CTkOptionMenu(
                item, variable=var, values=['全部'], width=100, height=28,
                font=ctk.CTkFont(size=12), command=self._apply_filter)
            menu.grid(row=0, column=1)
            self.location_vars[field] = var
            self.location_menus[field] = menu
            _fit_optionmenu(menu)
            self.location_items[field] = item
            item.grid_remove()   # 初始隐藏，采集到数据后显示

        # ---------- 中部：岗位列表 + 详情 ----------
        main_frame = ctk.CTkFrame(self, corner_radius=12)
        main_frame.grid(row=4, column=0, padx=20, pady=(0, 10), sticky='nsew')
        main_frame.grid_columnconfigure(0, weight=3)
        main_frame.grid_columnconfigure(1, weight=2)
        main_frame.grid_rowconfigure(0, weight=1)

        # 左侧：岗位列表（可点选）
        list_frame = ctk.CTkFrame(main_frame, fg_color='transparent')
        list_frame.grid(row=0, column=0, sticky='nsew', padx=(12, 6), pady=12)
        ctk.CTkLabel(list_frame, text='采集结果列表（点击岗位查看详情）', font=ctk.CTkFont(size=15, weight='bold')).pack(anchor='w', pady=(0, 6))
        self.job_list_box = ctk.CTkScrollableFrame(list_frame, width=620)
        self.job_list_box.pack(fill='both', expand=True)
        self._empty_hint = ctk.CTkLabel(
            self.job_list_box, text='暂无数据\n\n点击「开始采集」后，采集到的岗位会显示在这里',
            text_color='gray', font=ctk.CTkFont(size=14))
        self._empty_hint.pack(pady=40)

        # 右侧：岗位详情
        detail_frame = ctk.CTkFrame(main_frame, fg_color='transparent')
        detail_frame.grid(row=0, column=1, sticky='nsew', padx=(6, 12), pady=12)
        ctk.CTkLabel(detail_frame, text='岗位详情', font=ctk.CTkFont(size=15, weight='bold')).pack(anchor='w', pady=(0, 6))
        self.detail_box = ctk.CTkTextbox(detail_frame, width=430, wrap='word', font=ctk.CTkFont(size=14))
        self.detail_box.pack(fill='both', expand=True)
        self.detail_box.insert('1.0', '点击左侧岗位即可查看完整信息')
        self.detail_box.configure(state='disabled')

        # ---------- 操作按钮 ----------
        action_frame = ctk.CTkFrame(self, corner_radius=12)
        action_frame.grid(row=5, column=0, padx=20, pady=(0, 10), sticky='ew')
        self.export_selected_btn = ctk.CTkButton(
            action_frame, text='导出选中的岗位', font=ctk.CTkFont(size=14),
            width=150, height=34, command=self._export_selected
        )
        self.export_selected_btn.pack(side='left', padx=16, pady=10)
        self.export_all_btn = ctk.CTkButton(
            action_frame, text='导出全部岗位', font=ctk.CTkFont(size=14),
            width=150, height=34, command=self._export_all
        )
        self.export_all_btn.pack(side='left', padx=6, pady=10)
        self.export_filtered_btn = ctk.CTkButton(
            action_frame, text='导出当前列表', font=ctk.CTkFont(size=14),
            width=150, height=34, command=self._export_filtered
        )
        self.export_filtered_btn.pack(side='left', padx=6, pady=10)
        # 导出目录设置（可自定义，路径会自动记住）
        ctk.CTkLabel(action_frame, text='导出目录：', font=ctk.CTkFont(size=13)).pack(side='left', padx=(24, 0), pady=10)
        self.export_dir_label = ctk.CTkLabel(
            action_frame, text=self.export_dir, font=ctk.CTkFont(size=13),
            text_color='gray', width=300, anchor='w')
        self.export_dir_label.pack(side='left', padx=(0, 8), pady=10)
        self.export_dir_btn = ctk.CTkButton(
            action_frame, text='更改目录', font=ctk.CTkFont(size=13),
            width=90, height=32, command=self._choose_export_dir
        )
        self.export_dir_btn.pack(side='left', padx=(0, 16), pady=10)
        # 打开结果：一键打开导出目录中最近的 CSV 文件
        ctk.CTkButton(
            action_frame, text='打开结果', font=ctk.CTkFont(size=13, weight='bold'),
            width=90, height=32, command=self._open_latest_result,
            fg_color='#2e8b57', hover_color='#236b43'
        ).pack(side='left', padx=(0, 16), pady=10)
        ctk.CTkButton(
            action_frame, text='清空列表', font=ctk.CTkFont(size=14), width=110, height=34,
            fg_color='#b34747', hover_color='#8f3a3a', command=self._clear_jobs
        ).pack(side='right', padx=16, pady=10)

        # ---------- 日志区 ----------
        log_frame = ctk.CTkFrame(self, corner_radius=12)
        log_frame.grid(row=6, column=0, padx=20, pady=(0, 16), sticky='ew')
        ctk.CTkLabel(log_frame, text='运行日志', font=ctk.CTkFont(size=15, weight='bold')).pack(anchor='w', padx=16, pady=(10, 4))
        self.log_box = ctk.CTkTextbox(log_frame, height=230, font=ctk.CTkFont(size=13), state='disabled')
        self.log_box.pack(fill='x', padx=16, pady=(0, 12))
        self._log_ui('欢迎使用岗位采集助手！首次使用请点「打开登录页」登录招聘网站，然后点「开始采集」即可（浏览器会自动启动）。')

        # 收集需跟随主题变色的控件（标题已单独处理），并统一对齐到当前主题色
        self._theme_widgets = (
            [self.start_btn, self.site_menu, self.city_menu, self.keyword_menu, self.login_btn]
            + list(self.filter_menus.values())
            + list(self.location_menus.values())
            + list(self.multi_btns.values())
            + [self.export_selected_btn, self.export_all_btn, self.export_filtered_btn, self.export_dir_btn]
        )
        self._apply_theme()

    # ---------- 事件处理 ----------

    def _on_keyword_selected(self, value):
        self.keyword_var.set(value)
        # 选中岗位关键词后，自动刷新技能需求下拉框
        self._refresh_keyword_presets()

    def _get_skill_presets(self):
        """根据当前岗位关键词返回对应的技能预设；没有匹配时返回通用技能"""
        kw = self.keyword_var.get().strip()
        return KEYWORD_SKILL_PRESETS.get(kw, DEFAULT_SKILL_PRESETS)

    def _schedule_keyword_refresh(self):
        """输入关键词后延迟刷新（防抖，输入停顿约0.4秒后自动刷新）"""
        if hasattr(self, '_kw_timer') and self._kw_timer:
            try:
                self.after_cancel(self._kw_timer)
            except Exception:
                pass
        self._kw_timer = self.after(KEYWORD_DEBOUNCE_MS, self._refresh_keyword_presets)

    def _refresh_keyword_presets(self):
        """岗位关键词变化时，立即刷新技能需求下拉框（有预设显示预设，无预设显示全部）"""
        field = '技能需求'
        menu = self.filter_menus[field]
        options = ['全部'] + self._get_skill_presets()
        menu.configure(values=options)
        if self.filter_vars[field].get() not in options:
            self.filter_vars[field].set('全部')

    def _on_city_selected(self, value):
        """城市下拉框选中时同步到输入框"""
        self.city_var.set(value)

    def _refresh_city_menu(self):
        """按当前采集网站刷新城市下拉框：只显示一二线城市前20，其余可手动输入"""
        table = self.all_51job_cities if self.current_site == '51job' else self.all_cities
        # 只保留榜单中存在于当前城市表里的城市（保持榜单顺序）
        options = [name for name in TOP_CITY_NAMES if name in table]
        if not options:
            options = list(table.keys())[:20]
        current = self.city_var.get()
        if current and current not in options:
            options = [current] + list(options)
        self.city_menu.configure(values=options)

    def _apply_theme(self):
        """把界面上涉及主题色的控件整体刷新为当前主题色（BOSS 蓝 / 51job 橙）"""
        primary = self._theme['primary']
        hover = self._theme['hover']
        self.title_label.configure(text_color=primary)
        for w in self._theme_widgets:
            try:
                # CTkOptionMenu 是「左侧值显示区(fg_color) + 右侧箭头按钮(button_color)」两部分都得改
                if isinstance(w, ctk.CTkOptionMenu):
                    w.configure(fg_color=primary, button_color=primary,
                                button_hover_color=hover, text_color='white')
                else:
                    w.configure(fg_color=primary, hover_color=hover, text_color='white')
            except Exception:
                pass
        # 刷新列表选中态高亮（选中行用主题色，未选中行保持灰色）
        self._refresh_selection()

    def _on_site_selected(self, value):
        """切换采集网站：更新当前站点标记、主题色，并刷新城市下拉框"""
        self.current_site = '51job' if value.startswith('前程无忧') else 'boss'
        self._theme_key = self.current_site
        self._theme = THEME_COLORS[self._theme_key]
        self.city_var.set('北京')
        self._refresh_city_menu()
        self._apply_theme()
        self._log_ui(f'已切换采集网站：{value}（城市代码与界面配色已同步切换）')

    def _open_login_page(self):
        """在调试模式浏览器中打开当前采集网站的登录页，方便首次登录（后台执行，不阻塞界面）"""
        is_51job = self.current_site == '51job'
        site_label = '前程无忧(51job)' if is_51job else 'BOSS直聘'
        url = 'https://login.51job.com/login.htm' if is_51job else 'https://login.zhipin.com/'
        browser_name = BROWSERS[self.current_browser]['name']
        self._log_ui(f'▶ 正在 {browser_name} 调试浏览器中打开 {site_label} 登录页...')
        threading.Thread(target=self._do_open_login_page, args=(url, site_label, browser_name), daemon=True).start()

    def _do_open_login_page(self, url, site_label, browser_name):
        """后台确保浏览器已启动（显示到可见区域）并打开登录页"""
        ok, msg = ensure_debug_browser(visible=True)
        self.msg_queue.put(('log', f'· {msg}'))
        if not ok:
            return
        try:
            co = ChromiumOptions()
            co.debugger_address = CHROME_DEBUG_ADDR
            dp = ChromiumPage(co)
            dp.new_tab(url)
            self.msg_queue.put(('log', f'✅ 已在 {browser_name} 打开 {site_label} 登录页，请在浏览器窗口完成登录，登录后再回软件点「开始采集」。'))
        except Exception as e:
            exe = BROWSERS[self.current_browser]['exe']
            self.msg_queue.put(('log', f'⚠ 打开登录页失败：{type(e).__name__}: {e}'))
            self.msg_queue.put(('log', f'   请手动用调试模式启动浏览器（{exe} --remote-debugging-port=9222），再手动打开 {url} 登录。'))

    def _show_help(self):
        """弹窗展示一步一步的简易使用说明"""
        text = (
            '【使用说明 · 很简单】\n\n'
            '第 1 步：点「打开登录页」\n'
            '  软件会自动启动浏览器并打开登录页\n\n'
            '第 2 步：在浏览器里登录\n'
            '  · BOSS直聘：必须登录（手机号 / 扫码）\n'
            '  · 前程无忧：建议也登录一次\n\n'
            '第 3 步：点「开始采集」\n'
            '  选好网站、城市、关键词、页数，点开始采集即可\n\n'
            '完成后：\n'
            '  左侧点岗位看详情，点「导出」保存表格。\n\n'
            '小提示：\n'
            '  · 采集中弹滑块验证码，去浏览器滑一下即可继续\n'
            '  · 结果 CSV 和日志保存在软件所在目录\n'
        )
        messagebox.showinfo('使用说明', text)

    def _load_cities_async(self):
        """后台拉取城市数据（全国城市表 + 热门城市 + 省份映射），成功后更新下拉框（不阻塞界面）"""
        def worker():
            cities, hot_names, province_map = fetch_all_cities()
            self.msg_queue.put(('cities', (cities, hot_names, province_map)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_cities(self, cities, hot_names, province_map=None):
        if cities:
            self.all_cities = cities
            if province_map:
                self.province_map = province_map
            self._log_ui(f'✅ 已加载城市数据：下拉框显示一二线城市前 {len(TOP_CITY_NAMES)} 个，全部 {len(cities)} 个城市可手动输入')
        else:
            self._log_ui('⚠ 城市数据加载失败，使用内置热门城市（仍可直接输入城市名）')
        # 按当前采集网站刷新城市下拉框（BOSS 或 51job）
        self._refresh_city_menu()

    def _start_crawl(self):
        if self.crawling:
            return
        city = self.city_var.get().strip()
        keyword = self.keyword_var.get().strip()
        if not city:
            messagebox.showwarning('提示', '请选择或输入城市')
            return
        if not keyword:
            messagebox.showwarning('提示', '请填写岗位关键词')
            return
        try:
            pages = int(self.pages_var.get())
        except ValueError:
            messagebox.showwarning('提示', '采集页数请输入数字')
            return

        is_51job = self.current_site == '51job'
        site_label = '前程无忧(51job)' if is_51job else 'BOSS直聘'
        # 解析城市代码：两站城市代码体系不同，分别查找
        if is_51job:
            city_code = self.all_51job_cities.get(city)
        else:
            city_code = self.all_cities.get(city) or CITY_OPTIONS.get(city)
        if not city_code:
            messagebox.showwarning('提示', f'未找到城市「{city}」在{site_label}的城市代码，请从下拉列表中选择，或尝试输入其他写法（如「北京」）')
            return

        self.crawling = True
        self._stop_event.clear()
        self.start_btn.configure(state='disabled')
        # 开始新的采集时重置筛选条件：新数据全部采集不按筛选过滤，避免旧筛选把新岗位全部过滤导致列表看似为空
        self._clear_jobs(silent=True, reset_filters=True)
        browser_name = BROWSERS.get(self.current_browser, BROWSERS['chrome'])['name']
        self._log_ui(f'▶ 开始采集（{site_label} · {browser_name}）：{city} · {keyword}，共 {pages} 页（后台运行中，界面可正常操作）')

        worker = threading.Thread(
            target=self._worker, args=(city, city_code, keyword, pages, is_51job, self.current_browser), daemon=True)
        worker.start()

    def _stop_crawl(self):
        """请求停止当前采集（后台线程在翻页/处理岗位间隙检查并退出）"""
        if not self.crawling:
            self._log_ui('当前没有正在进行的采集任务')
            return
        self._stop_event.set()
        self._log_ui('⏹ 已请求停止采集，正在收尾（最多等待当前页/当前岗位处理完成）...')

    def _worker(self, city, city_code, keyword, pages, is_51job, browser='chrome'):
        try:
            # 采集前确保调试模式浏览器已启动（未启动则自动启动，并在后台隐藏运行）
            ok, msg = ensure_debug_browser(visible=False)
            self.msg_queue.put(('log', f'· {msg}'))
            if not ok:
                return
            if is_51job:
                crawl_51job(
                    city_name=city, city_code=city_code, keyword=keyword, total_pages=pages,
                    on_log=lambda m: self.msg_queue.put(('log', m)),
                    on_job=lambda j: self.msg_queue.put(('job', j)),
                    should_stop=self._stop_event.is_set, browser=browser)
            else:
                crawl_boss_zhipin(
                    city_name=city, city_code=city_code, keyword=keyword, total_pages=pages,
                    on_log=lambda m: self.msg_queue.put(('log', m)),
                    on_job=lambda j: self.msg_queue.put(('job', j)),
                    province_map=self.province_map,
                    should_stop=self._stop_event.is_set, browser=browser)
        except Exception as e:
            exe = BROWSERS.get(browser, BROWSERS['chrome'])['exe']
            self.msg_queue.put(('log', f'爬虫运行异常：{type(e).__name__}: {e}'))
            self.msg_queue.put(('log', f'排查建议：1) 是否已用调试模式启动浏览器（命令行运行 {exe} --remote-debugging-port=9222） 2) 是否有其他程序占用 9222 端口'))
            # 完整堆栈经日志队列写入界面与日志文件（双击运行无控制台时也不会丢失）
            self.msg_queue.put(('log', traceback.format_exc()))
        finally:
            self.msg_queue.put(('done', None))

    def _poll_queue(self):
        try:
            while True:
                kind, data = self.msg_queue.get_nowait()
                if kind == 'log':
                    self._log_ui(data)
                elif kind == 'job':
                    self._add_job(data)
                elif kind == 'cities':
                    self._apply_cities(data[0], data[1], data[2])
                elif kind == 'done':
                    self.crawling = False
                    self._stop_event.clear()
                    self.start_btn.configure(state='normal')
                    if not self.jobs:
                        exe = BROWSERS.get(self.current_browser, BROWSERS['chrome'])['exe']
                        browser_name = BROWSERS.get(self.current_browser, BROWSERS['chrome'])['name']
                        self._log_ui('⚠ 本次采集没有获取到任何岗位。排查建议：')
                        self._log_ui(f'   1) 是否已用调试模式启动 {browser_name}？命令行执行：{exe} --remote-debugging-port=9222')
                        self._log_ui(f'   2) {browser_name} 里是否已登录对应招聘网站（BOSS直聘需登录；51job 若触发验证码也需登录）')
                        self._log_ui('   3) 检查上方日志中每一页的采集情况，确认是否被风控或接口变更')
                    else:
                        self._log_ui(f'✅ 本次采集完成，共 {len(self.jobs)} 条岗位，可点选查看详情或导出')
                        if not self.filtered_jobs:
                            self._log_ui('⚠ 提示：当前筛选条件下没有匹配岗位，列表为空。可点击「重置筛选」查看全部岗位')
        except queue.Empty:
            pass
        self.after(POLL_INTERVAL_MS, self._poll_queue)

    # ---------- 列表与详情 ----------

    def _add_job(self, job):
        self.jobs.append(job)
        self._update_filter_options()
        # 采集到第一条数据后，显示省市区筛选
        if not self._location_shown:
            self._show_location_filters()
        # 符合当前筛选条件才加入可见列表
        if self._job_match_filters(job):
            idx = len(self.filtered_jobs)
            self.filtered_jobs.append(job)
            self._append_row(job, idx)
        name = job.get('岗位名称', '') or '(未命名岗位)'
        company = job.get('公司', '')
        self._log_ui(f'已采集：{name}（{company}）')

    def _append_row(self, job, idx):
        if self._empty_hint is not None:
            self._empty_hint.destroy()
            self._empty_hint = None
        name = job.get('岗位名称', '') or '(未命名岗位)'
        company = job.get('公司', '')
        salary = job.get('薪资')
        salary_raw = job.get('薪资原文', '')
        salary_text = salary_raw or (f'{salary:.0f}元/月' if salary else '面议')
        district = job.get('区', '')
        biz = job.get('商圈', '')
        place = '-'.join(x for x in [district, biz] if x)
        text = f'{name}  |  {company}  |  {salary_text}  |  {place}'

        btn = ctk.CTkButton(
            self.job_list_box, text=text, anchor='w', height=40,
            font=ctk.CTkFont(size=13), command=lambda i=idx: self._select_job(i),
            fg_color=('#e9e9e9', '#3a3a3a'),
            text_color=('#1a1a1a', '#dddddd'),
            hover_color=('#d5d5d5', '#4a4a4a'))
        btn.pack(fill='x', padx=4, pady=3)
        self.row_buttons.append(btn)

    # ---------- 筛选 ----------

    def _apply_filter(self, _=None):
        """任意筛选下拉框变化时调用，延迟重建可见列表（防抖，避免在事件回调中同步重建导致卡顿）"""
        if hasattr(self, '_filter_after_id') and self._filter_after_id:
            try:
                self.after_cancel(self._filter_after_id)
            except Exception:
                pass
        self._filter_after_id = self.after(FILTER_APPLY_DELAY_MS, self._rebuild_list)

    def _show_location_filters(self):
        """采集到数据后显示省市区筛选"""
        if not self._location_shown:
            for item in self.location_items.values():
                item.grid()
            self._location_shown = True

    def _hide_location_filters(self):
        """清空列表后隐藏省市区筛选"""
        if self._location_shown:
            for item in self.location_items.values():
                item.grid_remove()
            self._location_shown = False

    def _reset_filters(self):
        for var in self.filter_vars.values():
            var.set('全部')
        for var in self.location_vars.values():
            var.set('全部')
        for field in MULTI_SELECT_FIELDS:
            self.multi_selected[field].clear()
            self._update_multi_btn_text(field)
        self._rebuild_list()
        self._log_ui('已重置筛选条件')

    def _update_filter_options(self):
        """根据已采集数据刷新各筛选下拉框的选项（含省市区）"""
        all_fields = FILTER_FIELDS + LOCATION_FIELDS
        for field in all_fields:
            if field in MULTI_SELECT_FIELDS:
                # 点击多选字段：只更新可选列表（预设+实际数据），不重置用户已选项
                if field == '薪资':
                    # 薪资按区间档位筛选，不收集具体数值
                    options = list(PRESET_FILTER_OPTIONS['薪资'])
                else:
                    values = set()
                    for job in self.jobs:
                        v = job.get(field)
                        if isinstance(v, list):
                            values.update(str(x) for x in v if x)
                        elif v not in (None, ''):
                            values.add(str(v))
                    options = list(dict.fromkeys(
                        PRESET_FILTER_OPTIONS.get(field, []) + sorted(values)))
                self.multi_options[field] = options
                # 同步给内联下拉框（若正展开则刷新勾选项）
                self.multi_btns[field].set_options(options)
                # 若已选值不在选项中（如清空数据后），移除无效值并刷新按钮文字
                selected = self.multi_selected[field]
                old_len = len(selected)
                selected.intersection_update(options)
                if len(selected) != old_len:
                    self._update_multi_btn_text(field)
                continue
            if field == '技能需求':
                # 采集到数据后，技能需求用实际技能值覆盖预设；未采集时用关键词对应预设
                values = set()
                for job in self.jobs:
                    v = job.get('技能需求')
                    if isinstance(v, list):
                        values.update(str(x) for x in v if x)
                if values:
                    options = ['全部'] + sorted(values)
                else:
                    options = ['全部'] + self._get_skill_presets()
            else:
                values = set()
                for job in self.jobs:
                    v = job.get(field)
                    if isinstance(v, list):
                        values.update(str(x) for x in v if x)
                    elif v not in (None, ''):
                        values.add(str(v))
                options = list(dict.fromkeys(
                    ['全部'] + PRESET_FILTER_OPTIONS.get(field, []) + sorted(values)))
            menu = self.filter_menus.get(field) or self.location_menus[field]
            var = self.filter_vars.get(field) or self.location_vars[field]
            # 把当前选中的值补回选项列表：即使新采集的数据里暂未出现该值，也不清掉用户已配置的筛选
            current = var.get()
            if current not in ('', '全部') and current not in options:
                options = [current] + options
            menu.configure(values=options)
            # 若当前选中值不在选项中（如清空数据后），回退为"全部"
            if var.get() not in options:
                var.set('全部')

    def _update_multi_btn_text(self, field):
        """刷新内联多选下拉框的文字"""
        self.multi_btns[field].refresh_text()

    def _job_match_filters(self, job):
        """判断岗位是否满足当前全部筛选条件（多个条件同时生效，含省市区）"""
        filters = {f: (self.filter_vars.get(f) or self.location_vars[f]).get()
                   for f in FILTER_FIELDS + LOCATION_FIELDS if f not in MULTI_SELECT_FIELDS}
        return job_matches_filters(job, filters, self.multi_selected)

    def _rebuild_list(self):
        """按当前筛选条件重建整个可见列表"""
        for btn in self.row_buttons:
            btn.destroy()
        self.row_buttons.clear()
        self.selected_idx = None
        if self._empty_hint is not None:
            self._empty_hint.destroy()
            self._empty_hint = None

        self.filtered_jobs = [j for j in self.jobs if self._job_match_filters(j)]
        if not self.filtered_jobs:
            hint_text = ('暂无数据\n\n点击「开始采集」后，采集到的岗位会显示在这里'
                         if not self.jobs else
                         '没有符合当前筛选条件的岗位\n\n可点击「重置筛选」查看全部岗位')
            self._empty_hint = ctk.CTkLabel(
                self.job_list_box, text=hint_text, text_color='gray', font=ctk.CTkFont(size=14))
            self._empty_hint.pack(pady=40)
        else:
            for i, job in enumerate(self.filtered_jobs):
                self._append_row(job, i)
        self._refresh_selection()
        self._scroll_list_to_top()
        self._log_ui(f'筛选结果：{len(self.filtered_jobs)} / {len(self.jobs)} 条岗位')

    def _scroll_list_to_top(self):
        """重建列表后刷新滚动区域并回到顶部（避免新按钮渲染到可视区外）。用 getattr 安全访问内部画布，兼容不同 customtkinter 版本。"""
        canvas = getattr(self.job_list_box, '_parent_canvas', None)
        if canvas is None:
            return
        try:
            canvas.configure(scrollregion=canvas.bbox('all'))
            canvas.yview_moveto(0)
        except Exception:
            pass

    def _select_job(self, idx):
        self.selected_idx = idx
        self._refresh_selection()
        job = self.filtered_jobs[idx]
        lines = [f'【{job.get("岗位名称", "")}】', '']
        rows = [
            ('公司', '公司'), ('公司领域', '公司领域'), ('规模', '规模'),
            ('薪资原文', '薪资原文'), ('岗位链接', '岗位链接'),
            ('学历要求', '学历要求'), ('经验要求', '经验要求'),
            ('城市', '市'), ('区', '区'), ('商圈', '商圈'),
            ('技能需求', '技能需求'), ('福利待遇', '福利待遇'),
        ]
        for label, key in rows:
            value = job.get(key, '')
            if isinstance(value, list):
                value = '、'.join(str(v) for v in value)
            if value == '' or value is None:
                value = '-'
            lines.append(f'{label}：{value}')
        lon = job.get('经度', '-') or '-'
        lat = job.get('纬度', '-') or '-'
        lines.append(f'经度：{lon}    纬度：{lat}')
        # 职位描述：先清洗（合并多余空白行），再以小节形式展示
        desc = job.get('职位描述', '')
        if desc:
            # 去掉重复的标题行（DOM 提取时标题可能重复出现）
            desc = re.sub(r'^\s*职位描述\s*$', '', desc, flags=re.M)
            desc = re.sub(r'\n{3,}', '\n\n', desc).strip()
            lines.append('')
            lines.append('─' * 46)
            lines.append('【职位描述】')
            lines.append(desc)

        self.detail_box.configure(state='normal')
        self.detail_box.delete('1.0', 'end')
        self.detail_box.insert('1.0', '\n'.join(lines))
        self.detail_box.configure(state='disabled')

    def _refresh_selection(self):
        for i, btn in enumerate(self.row_buttons):
            if i == self.selected_idx:
                btn.configure(fg_color=self._theme['primary'], text_color='white', hover_color=self._theme['hover'])
            else:
                btn.configure(
                    fg_color=('#e9e9e9', '#3a3a3a'),
                    text_color=('#1a1a1a', '#dddddd'),
                    hover_color=('#d5d5d5', '#4a4a4a'))

    def _clear_jobs(self, silent=False, reset_filters=True):
        """清空已采集数据。
        reset_filters=True: 同时把筛选条件恢复为默认（清空列表按钮时用）
        reset_filters=False: 保留用户已配置的筛选条件（开始采集时用，避免白配筛选）"""
        self.jobs.clear()
        self.filtered_jobs.clear()
        self.selected_idx = None
        for btn in self.row_buttons:
            btn.destroy()
        self.row_buttons.clear()
        if reset_filters:
            # 重置筛选下拉框（公司领域/薪资/福利待遇为点击多选按钮，单独处理）
            for field in FILTER_FIELDS:
                if field in MULTI_SELECT_FIELDS:
                    continue
                self.filter_vars[field].set('全部')
                self.filter_menus[field].configure(values=['全部'])
            # 重置并隐藏省市区筛选
            for field in LOCATION_FIELDS:
                self.location_vars[field].set('全部')
                self.location_menus[field].configure(values=['全部'])
            for field in MULTI_SELECT_FIELDS:
                self.multi_selected[field].clear()
                self._update_multi_btn_text(field)
            self._hide_location_filters()
        if self._empty_hint is None:
            self._empty_hint = ctk.CTkLabel(
                self.job_list_box, text='暂无数据\n\n点击「开始采集」后，采集到的岗位会显示在这里',
                text_color='gray', font=ctk.CTkFont(size=14))
            self._empty_hint.pack(pady=40)
        self.detail_box.configure(state='normal')
        self.detail_box.delete('1.0', 'end')
        self.detail_box.insert('1.0', '点击左侧岗位即可查看完整信息')
        self.detail_box.configure(state='disabled')
        if not silent:
            self._log_ui('已清空列表')

    # ---------- 导出 ----------

    def _export_jobs(self, jobs, label):
        if not jobs:
            messagebox.showinfo('提示', f'{label}：暂无可导出的岗位数据')
            return
        # 若保存的导出目录已不存在，回退到默认目录
        export_dir = self.export_dir
        if not os.path.isdir(export_dir):
            export_dir = BASE_DIR
            self._log_ui(f'⚠ 导出目录不存在（{self.export_dir}），本次已改为默认目录')
        today = datetime.date.today().strftime('%Y%m%d')
        f, csv_name = open_csv_retry(f'导出岗位_{today}.csv', export_dir)
        with f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            for job in jobs:
                writer.writerow({k: sanitize_csv_value(job.get(k, '')) for k in CSV_FIELDNAMES})
        messagebox.showinfo('导出成功', f'已导出 {len(jobs)} 条岗位数据到：\n{csv_name}')
        self._log_ui(f'✅ 已导出 {len(jobs)} 条岗位数据到 {csv_name}')

    def _choose_export_dir(self):
        """弹出目录选择框，自定义导出路径，并记住该路径"""
        chosen = filedialog.askdirectory(initialdir=self.export_dir, title='选择导出目录')
        if not chosen:
            return
        self.export_dir = chosen
        self.export_dir_label.configure(text=chosen)
        save_settings({'export_dir': chosen})
        self._log_ui(f'✅ 导出目录已设置为：{chosen}（下次启动自动使用）')

    def _open_latest_result(self):
        """打开导出目录中最近生成的 CSV 文件（用系统默认程序打开）"""
        export_dir = self.export_dir
        if not os.path.isdir(export_dir):
            export_dir = BASE_DIR
        # 收集导出目录下所有 CSV，按修改时间取最新
        try:
            csv_files = [os.path.join(export_dir, f) for f in os.listdir(export_dir)
                         if f.lower().endswith('.csv')]
        except OSError as e:
            messagebox.showerror('打开失败', f'无法读取导出目录：{e}')
            return
        if not csv_files:
            messagebox.showinfo('提示', f'导出目录（{export_dir}）中没有找到 CSV 文件，请先采集或导出')
            return
        latest = max(csv_files, key=os.path.getmtime)
        try:
            # 用系统默认程序打开（Windows 下通常为 Excel / WPS）
            os.startfile(latest)
            self._log_ui(f'✅ 已打开最近的结果文件：{os.path.basename(latest)}')
        except Exception as e:
            messagebox.showerror('打开失败', f'打开文件失败：{e}')

    def _export_selected(self):
        if self.selected_idx is None:
            messagebox.showinfo('提示', '请先在左侧列表点选一条岗位')
            return
        self._export_jobs([self.filtered_jobs[self.selected_idx]], '选中的岗位')

    def _export_all(self):
        # 导出全部已采集岗位（不受筛选影响）
        self._export_jobs(self.jobs, '全部岗位')

    def _export_filtered(self):
        # 导出当前列表可见的岗位（即筛选后的结果）
        self._export_jobs(self.filtered_jobs, '当前列表的岗位')

    # ---------- 日志 ----------

    def _log_ui(self, msg):
        line = f'[{datetime.datetime.now():%H:%M:%S}] {msg}'
        self.log_box.configure(state='normal')
        self.log_box.insert('end', line + '\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')
        # 同时写入日志文件，方便排查问题（按日期滚动，追加写入）
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def _on_close(self):
        self.destroy()


if __name__ == '__main__':
    try:
        app = BossGuiApp()
        app.protocol('WM_DELETE_WINDOW', app._on_close)
        app.mainloop()
    except Exception:
        # 启动/运行崩溃时把错误写入文件，避免窗口闪退无法排查
        try:
            with open(os.path.join(BASE_DIR, '启动错误.txt'), 'w', encoding='utf-8') as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise
