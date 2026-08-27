# BOSS直聘岗位采集助手（图形界面版）
# 核心：页面操作 + 接口监听，附带 customtkinter 现代风格界面
import os
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 设置文件：保存导出目录等用户偏好
SETTINGS_FILE = os.path.join(BASE_DIR, '设置.json')
# 运行日志目录：按日期滚动保存，避免历史日志被覆盖（不再使用固定的 '运行日志.txt'）
LOG_DIR = os.path.join(BASE_DIR, 'logs')
LOG_FILE = os.path.join(LOG_DIR, f'运行日志_{datetime.date.today().strftime("%Y%m%d")}.txt')

# ==================== 运行参数（集中管理魔法数字） ====================
CHROME_DEBUG_ADDR = '127.0.0.1:9222'   # 调试模式 Chrome 的地址
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
                      on_log=None, on_job=None, province_map=None):
    """采集BOSS直聘岗位数据。
    city_code: BOSS直聘城市代码（可为空，为空时从热门城市表自动查找）
    province_map: 城市名->省份 映射（用于补全省份信息，可为空）
    on_log: 日志回调(接收字符串)
    on_job: 每采集到一条岗位的回调(接收dict)
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

        # 2. 连接调试模式 Chrome（需要先启动）
        say(f'正在连接调试模式 Chrome（{CHROME_DEBUG_ADDR}）...')
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
        for page in range(1, total_pages + 1):
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
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        popup = ctk.CTkToplevel(root)
        popup.overrideredirect(True)
        popup.attributes('-topmost', True)
        popup.geometry(f'{self.winfo_width()}x{self.POPUP_HEIGHT}+{x}+{y}')
        box = ctk.CTkScrollableFrame(popup, width=self.winfo_width(), height=self.POPUP_HEIGHT)
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
                                 font=self._font, command=lambda o=opt: self._on_item(o))
            cb.pack(anchor='w', pady=3, padx=10)
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

        self.title('BOSS直聘岗位采集助手')
        # 窗口宽度固定；高度自动适配屏幕，避免超出屏幕导致内容被压缩看不见
        screen_h = self.winfo_screenheight()
        win_h = min(1294, max(760, screen_h - 80))
        self.geometry(f'1182x{win_h}')
        self.resizable(False, False)

        self.msg_queue = queue.Queue()   # 后台线程 -> 界面 的消息队列
        self.jobs = []                   # 已采集的岗位数据列表（全部）
        self.filtered_jobs = []          # 筛选后可见的岗位列表
        self.row_buttons = []            # 列表区每一行的按钮控件
        self.selected_idx = None         # 当前选中的行号（对应 filtered_jobs）
        self.crawling = False            # 是否正在采集
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

    # ---------- 界面搭建 ----------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # 顶部标题
        ctk.CTkLabel(
            self, text='BOSS直聘 岗位采集助手',
            font=ctk.CTkFont(size=28, weight='bold'), text_color='#1f6aa5'
        ).grid(row=0, column=0, pady=(18, 2))
        ctk.CTkLabel(
            self, text='自动采集岗位信息 · 点选查看详情 · 一键导出表格',
            font=ctk.CTkFont(size=14), text_color='gray'
        ).grid(row=1, column=0, pady=(0, 6))

        # ---------- 参数区 ----------
        param_frame = ctk.CTkFrame(self, corner_radius=12)
        param_frame.grid(row=2, column=0, padx=20, pady=10, sticky='ew')

        ctk.CTkLabel(param_frame, text='选择城市：', font=ctk.CTkFont(size=15)).grid(row=0, column=0, padx=(16, 4), pady=14)
        self.city_var = ctk.StringVar(value='北京')
        self.city_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.city_var, values=list(CITY_OPTIONS.keys()),
            width=110, font=ctk.CTkFont(size=14), command=self._on_city_selected
        )
        self.city_menu.grid(row=0, column=1, padx=(0, 8), pady=14)
        ctk.CTkEntry(
            param_frame, textvariable=self.city_var, width=110,
            placeholder_text='可自定义输入', font=ctk.CTkFont(size=14)
        ).grid(row=0, column=2, padx=(0, 14), pady=14)

        ctk.CTkLabel(param_frame, text='岗位关键词：', font=ctk.CTkFont(size=15)).grid(row=0, column=3, padx=(0, 4), pady=14)
        self.keyword_var = ctk.StringVar(value='游戏测试')
        self.keyword_menu = ctk.CTkOptionMenu(
            param_frame, variable=self.keyword_var, values=KEYWORD_OPTIONS,
            width=130, font=ctk.CTkFont(size=14), command=self._on_keyword_selected
        )
        self.keyword_menu.grid(row=0, column=4, padx=(0, 8), pady=14)
        self.keyword_entry = ctk.CTkEntry(
            param_frame, textvariable=self.keyword_var, width=130,
            placeholder_text='可自定义输入', font=ctk.CTkFont(size=14))
        self.keyword_entry.grid(row=0, column=5, padx=(0, 14), pady=14)

        ctk.CTkLabel(param_frame, text='采集页数：', font=ctk.CTkFont(size=15)).grid(row=0, column=6, padx=(0, 4), pady=14)
        self.pages_var = ctk.StringVar(value='5')
        ctk.CTkEntry(param_frame, textvariable=self.pages_var, width=60, font=ctk.CTkFont(size=14)).grid(row=0, column=7, pady=14)

        self.start_btn = ctk.CTkButton(
            param_frame, text='开始采集', font=ctk.CTkFont(size=16, weight='bold'),
            width=125, height=38, command=self._start_crawl
        )
        self.start_btn.grid(row=0, column=8, padx=14, pady=12)

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
        ctk.CTkButton(
            action_frame, text='导出选中的岗位', font=ctk.CTkFont(size=14),
            width=150, height=34, command=self._export_selected
        ).pack(side='left', padx=16, pady=10)
        ctk.CTkButton(
            action_frame, text='导出全部岗位', font=ctk.CTkFont(size=14),
            width=150, height=34, command=self._export_all
        ).pack(side='left', padx=6, pady=10)
        ctk.CTkButton(
            action_frame, text='导出当前列表', font=ctk.CTkFont(size=14),
            width=150, height=34, command=self._export_filtered
        ).pack(side='left', padx=6, pady=10)
        # 导出目录设置（可自定义，路径会自动记住）
        ctk.CTkLabel(action_frame, text='导出目录：', font=ctk.CTkFont(size=13)).pack(side='left', padx=(24, 0), pady=10)
        self.export_dir_label = ctk.CTkLabel(
            action_frame, text=self.export_dir, font=ctk.CTkFont(size=13),
            text_color='gray', width=300, anchor='w')
        self.export_dir_label.pack(side='left', padx=(0, 8), pady=10)
        ctk.CTkButton(
            action_frame, text='更改目录', font=ctk.CTkFont(size=13),
            width=90, height=32, command=self._choose_export_dir
        ).pack(side='left', padx=(0, 16), pady=10)
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
        self._log_ui('欢迎使用 BOSS直聘岗位采集助手！请先按说明用调试模式打开 Chrome，再开始采集。')

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
            # 下拉框只显示热门城市（约10个），其余城市可手动输入
            options = hot_names or sorted(cities.keys())
            current = self.city_var.get()
            if current and current not in options:
                options = [current] + list(options)
            self.city_menu.configure(values=options)
            self._log_ui(f'✅ 已加载城市数据：下拉框显示 {len(options)} 个热门城市，全部 {len(cities)} 个城市可手动输入')
        else:
            self._log_ui('⚠ 城市数据加载失败，使用内置热门城市（仍可直接输入城市名）')

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

        # 解析城市代码：优先全国城市表，其次内置热门城市
        city_code = self.all_cities.get(city) or CITY_OPTIONS.get(city)
        if not city_code:
            messagebox.showwarning('提示', f'未找到城市「{city}」的城市代码，请从下拉列表中选择，或尝试输入其他写法（如「北京」）')
            return

        self.crawling = True
        self.start_btn.configure(state='disabled')
        # 开始新的采集时重置筛选条件：新数据全部采集不按筛选过滤，避免旧筛选把新岗位全部过滤导致列表看似为空
        self._clear_jobs(silent=True, reset_filters=True)
        self._log_ui(f'▶ 开始采集：{city} · {keyword}，共 {pages} 页（后台运行中，界面可正常操作）')

        worker = threading.Thread(
            target=self._worker, args=(city, city_code, keyword, pages), daemon=True)
        worker.start()

    def _worker(self, city, city_code, keyword, pages):
        try:
            crawl_boss_zhipin(
                city_name=city, city_code=city_code, keyword=keyword, total_pages=pages,
                on_log=lambda m: self.msg_queue.put(('log', m)),
                on_job=lambda j: self.msg_queue.put(('job', j)),
                province_map=self.province_map)
        except Exception as e:
            self.msg_queue.put(('log', f'爬虫运行异常：{type(e).__name__}: {e}'))
            self.msg_queue.put(('log', '排查建议：1) 是否已用调试模式启动 Chrome（命令行运行 chrome.exe --remote-debugging-port=9222） 2) 是否有其他程序占用 9222 端口'))
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
                    self.start_btn.configure(state='normal')
                    if not self.jobs:
                        self._log_ui('⚠ 本次采集没有获取到任何岗位。排查建议：')
                        self._log_ui('   1) 是否已用调试模式启动 Chrome？命令行执行：chrome.exe --remote-debugging-port=9222')
                        self._log_ui('   2) Chrome 里是否已登录 BOSS直聘（未登录或触发验证码会无数据）')
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
            font=ctk.CTkFont(size=13), command=lambda i=idx: self._select_job(i))
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
                btn.configure(fg_color='#1f6aa5', text_color='white', hover_color='#144870')
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
