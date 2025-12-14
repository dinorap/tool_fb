import time
import random
from playwright.sync_api import sync_playwright
import json
import re
from urllib.parse import urlparse, parse_qs, unquote
import os

# ==============================================================================
# JS TOOLS & HELPER FUNCTIONS
# ==============================================================================
JS_EXPAND_SCRIPT = """
(node) => {
    if (!node) return 0;
    const keywords = ["Xem thêm", "See more"];
    let clickedCount = 0;
    const buttons = node.querySelectorAll('[role="button"]');
    buttons.forEach(btn => {
        const text = btn.innerText ? btn.innerText.trim() : "";
        if (keywords.includes(text)) {
            if (btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                btn.scrollIntoView({block: "center", inline: "nearest"});
                btn.click();
                clickedCount++;
                btn.style.border = "2px solid red";
            }
        }
    });
    return clickedCount;
}
"""

JS_CHECK_AND_HIGHLIGHT_SCOPED = """
([node, keywords]) => { 
    if (!node || !keywords || keywords.length === 0) return false;
    const strictSelectors = [
        '[data-ad-preview="message"]',              
        '[data-ad-rendering-role="story_message"]', 
        '.userContent'                              
    ];
    let targetScope = null;
    for (const selector of strictSelectors) {
        const found = node.querySelector(selector);
        if (found) {
            targetScope = found;
            break;
        }
    }
    if (!targetScope) return false;

    const sortedKeywords = keywords.sort((a, b) => b.length - a.length);
    const pattern = new RegExp(`(${sortedKeywords.join('|')})`, 'gi');
    let foundCount = 0;
    function highlightTextNode(textNode) {
        const text = textNode.nodeValue;
        if (!pattern.test(text)) return;
        const fragment = document.createDocumentFragment();
        const parts = text.split(pattern);
        parts.forEach(part => {
            if (pattern.test(part)) {
                const span = document.createElement('span');
                Object.assign(span.style, {
                    backgroundColor: 'yellow', color: 'red', fontWeight: 'bold',
                    border: '2px solid red', padding: '2px', zIndex: '9999'
                });
                span.innerText = part;
                fragment.appendChild(span);
                foundCount++;
            } else {
                fragment.appendChild(document.createTextNode(part));
            }
            pattern.lastIndex = 0; 
        });
        textNode.parentNode.replaceChild(fragment, textNode);
    }
    const walker = document.createTreeWalker(targetScope, NodeFilter.SHOW_TEXT, {
        acceptNode: n => {
            if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'BUTTON', 'INPUT'].includes(n.parentNode.nodeName)) {
                return NodeFilter.FILTER_REJECT;
            }
            if (n.parentNode.isContentEditable) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_ACCEPT;
        }
    });
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    textNodes.forEach(highlightTextNode);
    return foundCount > 0;
}
"""

def extract_facebook_post_id(url: str):
    if not url: return None
    try: url = unquote(url)
    except: pass
    
    # Ưu tiên tìm pfbid trước
    patterns = [
        r"(pfbid[A-Za-z0-9]+)", 
        r"/posts/(\d+)", 
        r"/videos/(\d+)", 
        r"/reel/(\d+)",
        r"story_fbid=(\d+)", 
        r"fbid=(\d+)",
        r"id=(\d+)"
    ]
    for p in patterns:
        m = re.search(p, url)
        if m: return m.group(1)
        
    qs = parse_qs(urlparse(url).query)
    for k in ["story_fbid", "fbid", "id"]:
        if k in qs: return qs[k][0]
    return None

def parse_graphql_payload(post_data):
    """Phân tích data gửi đi để tìm biến 'url' trong payload."""
    if not post_data: return None
    variables_str = None
    try:
        if isinstance(post_data, str):
            json_body = json.loads(post_data)
        else:
            json_body = post_data
        variables_str = json.dumps(json_body.get("variables", {}))
    except:
        try:
            qs = parse_qs(post_data)
            if "variables" in qs:
                variables_str = qs["variables"][0]
        except: pass

    # Tìm các loại URL phổ biến trong payload
    if variables_str:
        # 1. Tìm key "url": "..."
        match = re.search(r'"url"\s*:\s*"([^"]+)"', variables_str)
        if match: return match.group(1).replace(r"\/", "/")
        
        # 2. Tìm key "shareable_url": "..."
        match2 = re.search(r'"shareable_url"\s*:\s*"([^"]+)"', variables_str)
        if match2: return match2.group(1).replace(r"\/", "/")

    return None

class FBController:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.browser = None
        self.page = None
        self.play = None
        self.profile_id = "unknown"
        
        self.captured_response_id = None 
        self.captured_payload_url = None # Biến chứa Link để soi view-source
        
        self.job_keywords = [
            "tuyển dụng", "tuyển nhân viên", "tuyển gấp", "việc làm", 
            "lương", "thu nhập", "phỏng vấn", "cv", "hồ sơ",
            "full-time", "part-time", "thực tập", "kế toán", "may mặc", "kcn",
            "Ứng viên ", "Ứng tuyển"
        ]

    def connect(self):
        self.play = sync_playwright().start()
        self.browser = self.play.chromium.connect_over_cdp(self.ws_url)
        context = self.browser.contexts[0]
        self.page = context.pages[0]
        
        # Ép cứng độ phân giải để tránh lỗi mất nút Share

        
        self.start_network_sniffer()
        
        try:
            # Click vào giữa màn hình để active window
            viewport = self.page.viewport_size
            self.page.mouse.click(viewport['width']/2, viewport['height']/2)
        except: pass

    def goto(self, url):
        self.page.goto(url, timeout=0)
        # Zoom out để hiện full giao diện
       

    def smooth_scroll_to(self, element):
        """Cuộn mượt đến element"""
        try:
            box = element.bounding_box()
            if not box: return
            target_y = box['y'] + self.page.evaluate("window.scrollY") - (self.page.viewport_size['height'] / 2)
            self.page.evaluate(f"window.scrollTo({{top: {target_y}, behavior: 'smooth'}});")
            time.sleep(1.0)
        except:
            element.scroll_into_view_if_needed()

    # ===================== [CORE] NETWORK SNIFFER =====================
    def start_network_sniffer(self):
        print("🛰  Đã kích hoạt Sniffer: Chế độ Response > Payload URL...")

        # 1. BẮT URL TỪ REQUEST (DỰ PHÒNG CHO VIEW-SOURCE)
        def on_request(request):
            if "facebook.com/api/graphql" in request.url and request.method == "POST":
                try:
                    raw_url = parse_graphql_payload(request.post_data)
                    if raw_url:
                        # Chỉ lưu nếu nó giống link bài viết
                        if "facebook.com" in raw_url or "pfbid" in raw_url:
                            self.captured_payload_url = raw_url
                            # print(f"🔗 [DEBUG] Bắt được Link tiềm năng: {raw_url[:50]}...")
                except: pass

        # 2. BẮT ID TỪ RESPONSE (ƯU TIÊN TUYỆT ĐỐI)
        def on_response(response):
            if "facebook.com/api/graphql" in response.url and response.status == 200:
                if not self.captured_response_id:
                    try:
                        data = response.json()
                        preview_data = data.get("data", {}).get("xma_preview_data", {})
                        pid = preview_data.get("post_id")
                        if pid:
                            self.captured_response_id = str(pid)
                            print(f"🎯 [RES-Json] Bắt dính ID CHÍNH THỨC: {self.captured_response_id}")
                    except: pass

        self.page.on("request", on_request)
        self.page.on("response", on_response)

    # ===================== [MỚI] HÀM SOI VIEW-SOURCE =====================
    def get_id_blocking_mode(self, url):
        """
        Mở tab mới -> Soi Code -> Tìm chữ "post_id" đầu tiên -> Trả về ngay.
        """
        print(f"⛔ [BLOCKING] Tạm dừng để soi source URL: {url}")
        new_page = None
        found_id = None
        
        try:
            context = self.page.context
            # 1. Mở tab mới
            new_page = context.new_page()
            
            # 2. Truy cập view-source (Treo bot ở đây chờ tải xong mới chạy tiếp)
            target = f"view-source:{url}"
            print("    -> Đang tải source code (Chờ DOMContentLoaded)...")
            new_page.goto(target, wait_until='domcontentloaded', timeout=20000)
            
            # 3. Lấy toàn bộ HTML
            content = new_page.content()
            
            # 4. TÌM KIẾM CHÍNH XÁC "post_id"
            # re.search mặc định sẽ quét từ trên xuống dưới và trả về kết quả ĐẦU TIÊN nó thấy.
            # Đúng ý Sếp: Thấy cái đầu là chốt luôn.
            
            # Pattern 1: Dạng chuẩn "post_id":"12345"
            match = re.search(r'"post_id":"(\d+)"', content)
            
            if match:
                found_id = match.group(1)
                print(f"    -> 💉 BẮT ĐƯỢC ID ĐẦU TIÊN (post_id): {found_id}")
            else:
                # Fallback: Nếu không thấy "post_id" thì mới tìm "story_fbid" (dự phòng)
                match_sub = re.search(r'"story_fbid":"(\d+)"', content)
                if match_sub:
                    found_id = match_sub.group(1)
                    print(f"    -> 💉 Không có post_id, lấy tạm story_fbid: {found_id}")

            if not found_id:
                print("    -> ⚠️ Không tìm thấy ID nào trong source.")

        except Exception as e:
            print(f"    -> ❌ Lỗi khi soi source: {e}")
        finally:
            # 5. Đóng tab ngay lập tức
            if new_page: 
                new_page.close()
                print("    -> Đã đóng tab soi code. Quay lại tab chính...")
                
        return found_id

    # ===================== SHARE & CHỜ ID (LOGIC UPDATE) =====================
    def share_center_ad(self, post_handle):
        try:
            print("🚀 Đang thực hiện share để bắt ID...")
            
            # 1. Reset biến
            self.captured_response_id = None
            self.captured_payload_url = None 
            
            # 2. Click nút Share (Trượt êm)
            xpath_selector = 'xpath=.//div[@data-ad-rendering-role="share_button"]/ancestor::div[@role="button"]'
            share_btn = post_handle.query_selector(xpath_selector)
            
            if share_btn:
                self.smooth_scroll_to(share_btn)
                self.page.wait_for_timeout(500) 
                share_btn.click()
                print("✅ Đã click nút Share. Đang chờ Server phản hồi...")
                
                # 3. Vòng lặp chờ ID từ Server (Chờ 5 giây thôi)
                for i in range(25): 
                    if self.captured_response_id:
                        print(f"🎉 SUCCESS: Server trả ID chuẩn: {self.captured_response_id}")
                        self.save_post_id(self.captured_response_id)
                        
                        self.page.wait_for_timeout(2000)
                        self.page.keyboard.press("Escape")
                        return True
                    self.page.wait_for_timeout(200)
                
                # 4. SERVER KHÔNG TRẢ -> KÍCH HOẠT CHẾ ĐỘ VIEW-SOURCE (BLOCKING)
                print("⚠️ Server không trả ID. Kiểm tra URL dự phòng...")
                
                if self.captured_payload_url:
                    print(f"💡 Có link trong Payload: {self.captured_payload_url}")
                    
                    # Gọi hàm này là bot sẽ TỰ ĐỘNG DỪNG mọi việc khác để chờ
                    source_id = self.get_id_blocking_mode(self.captured_payload_url)
                    
                    if source_id:
                        self.save_post_id(source_id)
                        self.page.wait_for_timeout(1000)
                        self.page.keyboard.press("Escape")
                        return True
                else:
                    print("⚠️ Không bắt được cả URL Link -> Bó tay.")

                # 5. Thất bại
                print("⚠️ SKIP: Không lấy được ID.")
                self.page.keyboard.press("Escape") 
                return False
            else:
                print("⚠️ Không tìm thấy nút Share.")
                return False
                
        except Exception as e:
            print(f"❌ Lỗi share_center_ad: {e}")
            self.page.keyboard.press("Escape")
            return False

    def save_post_id(self, post_id):
        try:
            folder = "data/post_ids"
            os.makedirs(folder, exist_ok=True)
            filepath = f"{folder}/{self.profile_id}.json"
            data = []
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf8") as f: data = json.load(f)
                except: pass
            if post_id in data:
                print("🔁 ID trùng -> bỏ qua.")
                return False
            data.append(post_id)
            with open(filepath, "w", encoding="utf8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"💾 Đã lưu ID {post_id} vào file.")
            return True
        except: return False

    def scroll_percent(self, ratio=0.5):
        """Cuộn trang theo % chiều cao"""
        try:
            viewport = self.page.viewport_size
            height = viewport['height'] if viewport else 800
            scroll_distance = int(height * ratio)
            # print(f"⬇️ Cuộn {int(ratio*100)}%...")
            self.page.mouse.wheel(0, scroll_distance)
            return True
        except: return False

    def scan_while_scrolling(self):
        try:
            viewport = self.page.viewport_size
            height = viewport['height'] if viewport else 800
            total_distance = int(height * 0.6) 
            steps = random.randint(15, 25)
            step_size = total_distance / steps
            
            print(f"⬇️ Đang lướt {total_distance}px...")

            for i in range(steps):
                self.page.mouse.wheel(0, step_size)
                time.sleep(random.uniform(0.03, 0.08)) 
                
                if i > 0 and i % 4 == 0:
                    current_post = self.get_center_post()
                    if current_post and self.check_current_post_is_ad(current_post):
                        print(f"🛑 BẮT ĐƯỢC ADS! (Tại bước {i}/{steps})")
                        return current_post
            
            time.sleep(random.uniform(2.0, 3.5))
            return None
        except Exception as e:
            try: self.page.keyboard.press("PageDown"); time.sleep(2)
            except: pass
            return None

    def like_current_post(self, post_handle):
        print("❤️ Đang thực hiện Like...")
        try:
            element = post_handle.as_element()
            if not element: return False
            already_liked = element.query_selector('div[role="button"][aria-label="Gỡ Thích"], div[role="button"][aria-label="Remove Like"]')
            if already_liked:
                print("⚠️ Đã Like rồi.")
                return False
            selector = 'div[role="button"][aria-label="Thích"], div[role="button"][aria-label="Like"]'
            like_btn = element.query_selector(selector)
            if like_btn:
                self.smooth_scroll_to(like_btn)
                like_btn.click()
                print("✅ Like thành công!")
                return True
            return False
        except: return False

    def process_ad_content(self, post_handle):
        try:
            expanded = self.page.evaluate(JS_EXPAND_SCRIPT, post_handle)
            if expanded > 0: time.sleep(1.0)
            has_keyword = self.page.evaluate(JS_CHECK_AND_HIGHLIGHT_SCOPED, [post_handle, self.job_keywords])
            if has_keyword:
                print("    -> ✅ FOUND: Bài Ads chứa từ khóa!")
                return True
            else:
                print("    -> ❌ SKIP: Không thấy từ khóa tuyển dụng.")
                return False
        except: return False

    def get_center_post(self):
        try:
            return self.page.evaluate_handle("""
                () => {
                    const x = window.innerWidth / 2;
                    const y = window.innerHeight * 0.45;
                    let el = document.elementFromPoint(x, y);
                    if (!el) return null;
                    const post = el.closest('div[role="article"], div.x1lliihq');
                    if (post) {
                        post.style.outline = "3px solid #00ff00";
                        return post;
                    }
                    return null;
                }
            """)
        except: return None

    def check_current_post_is_ad(self, post_handle):
        if not post_handle or not post_handle.as_element(): return False
        return post_handle.evaluate("""
            (post) => {
                if (post.getAttribute('data-bot-processed') === 'true') return false;
                const checkAnchors = (element) => {
                    if (!element) return false;
                    const anchors = Array.from(element.querySelectorAll('a[href*="__cft__"]'));
                    for (const a of anchors) {
                        const href = a.getAttribute('href');
                        if (!href) continue;
                        if (href.includes('__tn__')) continue;
                        let m = href.match(/__cft__\\[0\\]=([^&#]+)/) || href.match(/__cft__%5B0%5D=([^&#]+)/);
                        if (m && m[1]) return true; 
                    }
                    return false;
                };
                if (checkAnchors(post)) { post.style.outline = "5px solid red"; return true; }
                if (post.parentElement && checkAnchors(post.parentElement)) { post.style.outline = "5px solid red"; return true; }
                if (post.parentElement && post.parentElement.parentElement && checkAnchors(post.parentElement.parentElement)) { post.style.outline = "5px solid red"; return true; }
                return false;
            }
        """)

    def mark_post_as_processed(self, post_handle):
        try:
            post_handle.evaluate("""(post) => {
                post.setAttribute('data-bot-processed', 'true');
                post.style.outline = "5px solid gray"; 
                post.style.opacity = "0.7";
            }""")
            # print("🏁 Đã đánh dấu bài viết: DONE.")
        except: pass