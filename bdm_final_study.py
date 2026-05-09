"""
BDM FINAL STUDY — exam prep game.

Modes:
  LEARN  — see the source slide first, then quiz yourself (no timer).
  DRILL  — timed + spaced repetition. Use to retrieve under pressure.
  BOSS   — 25-question gauntlet, 10 HP, mistakes hurt.

Run:        python bdm_final_study.py
Mobile:     python bdm_final_study.py            (auto-detected on phones)
            python bdm_final_study.py --mobile   (forces narrow layout)
            python bdm_final_study.py --desktop  (forces wide layout)

Files needed in same folder:
  bdm_definitions.json
  bdm_sql.json
  slides/                       (folder of PNG slide images, optional)
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Union

try:
    import pygame
except ImportError as exc:
    raise SystemExit("Install pygame first: pip install pygame") from exc


# -------------------------------------------------------------------
# DATA
# -------------------------------------------------------------------
@dataclass
class Question:
    id: int
    topic_group: str
    topic: str
    difficulty: int
    type: str
    prompt: str
    options: List[str]
    answer: Union[int, List[int]]
    explanation: str = ""
    slideRef: str = ""
    bank: str = "def"


# -------------------------------------------------------------------
# PERSISTENCE
# -------------------------------------------------------------------
class DBManager:
    def __init__(self, db_name="bdm_study_data.db"):
        self.conn = sqlite3.connect(db_name)
        self.create_tables()

    def create_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS progress (
                question_id INTEGER PRIMARY KEY,
                streak INTEGER DEFAULT 0,
                next_review REAL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS answer_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                topic_group TEXT,
                correct INTEGER
            )
        """)
        self.conn.commit()

    def update_progress(self, question_id, topic_group, correct):
        cur = self.conn.cursor()
        cur.execute("SELECT streak FROM progress WHERE question_id=?", (question_id,))
        row = cur.fetchone()
        streak = row[0] if row else 0
        if correct:
            streak += 1
            intervals = {1: 30, 2: 180, 3: 1800, 4: 14400, 5: 86400}
            delay = intervals.get(streak, 86400)
        else:
            streak = 0
            delay = 0
        next_review = time.time() + delay
        cur.execute("""
            INSERT INTO progress (question_id, streak, next_review)
            VALUES (?, ?, ?)
            ON CONFLICT(question_id) DO UPDATE SET
                streak = excluded.streak,
                next_review = excluded.next_review
        """, (question_id, streak, next_review))
        cur.execute("""
            INSERT INTO answer_history (timestamp, topic_group, correct)
            VALUES (?, ?, ?)
        """, (time.time(), topic_group, 1 if correct else 0))
        self.conn.commit()

    def get_topic_accuracy(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT topic_group, AVG(correct), COUNT(*)
            FROM answer_history
            GROUP BY topic_group
        """)
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    def reset_progress(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM progress")
        cur.execute("DELETE FROM answer_history")
        self.conn.commit()


# -------------------------------------------------------------------
# COLORS
# -------------------------------------------------------------------
class C:
    BG          = (22, 25, 35)
    PANEL       = (15, 18, 25)
    PANEL_LINE  = (40, 50, 70)
    TEXT        = (225, 225, 225)
    TEXT_DIM    = (155, 155, 155)
    TEXT_FAINT  = (105, 105, 105)
    GREEN       = (0, 255, 150)
    RED         = (255, 80, 80)
    YELLOW      = (255, 215, 0)
    BLUE        = (100, 200, 255)
    PURPLE      = (180, 120, 255)
    ORANGE      = (255, 170, 90)
    BTN_BG      = (45, 50, 70)
    BTN_HOV     = (70, 80, 110)
    BTN_SEL_BG  = (30, 100, 70)
    BTN_SEL_HOV = (40, 120, 90)


# -------------------------------------------------------------------
# DEVICE DETECTION
# -------------------------------------------------------------------
def detect_device(force=None):
    """
    Returns ('mobile' | 'desktop', (width, height)) for the actual device.
    Strategy:
      1. Honor explicit --mobile / --desktop override.
      2. Look at the actual display resolution via pygame.display.Info().
      3. If the display is portrait-oriented OR narrow (< 900 px wide), treat
         as mobile and use the device's full screen size.
      4. Otherwise, desktop with a 1200x800 window.
    """
    pygame.init()
    info = pygame.display.Info()
    dw, dh = info.current_w, info.current_h

    if force == "desktop":
        return "desktop", (1200, 800)
    if force == "mobile":
        # Use real screen if we have one, else fall back to a sensible phone size
        if dw > 0 and dh > 0:
            return "mobile", (dw, dh)
        return "mobile", (430, 760)

    # Auto-detect: phones report tall narrow displays, desktops report wide
    if dw <= 0 or dh <= 0:
        return "desktop", (1200, 800)

    is_portrait = dh > dw
    is_narrow = dw < 900

    if is_portrait or is_narrow:
        return "mobile", (dw, dh)
    return "desktop", (1200, 800)


# -------------------------------------------------------------------
# GAME
# -------------------------------------------------------------------
class BDMFinalStudy:
    def __init__(self, force_mode=None):
        device, (w, h) = detect_device(force=force_mode)
        self.device = device

        flags = pygame.RESIZABLE
        if device == "mobile":
            # SCALED keeps the surface readable on high-DPI screens
            flags |= pygame.SCALED
            # On very tall phones, also try fullscreen so the keyboard/nav bars don't crop
            # (but keep RESIZABLE so user can still rotate)
            flags |= pygame.FULLSCREEN

        try:
            self.screen = pygame.display.set_mode((w, h), flags)
        except pygame.error:
            # Fullscreen sometimes fails on Pydroid; fall back gracefully
            self.screen = pygame.display.set_mode((w, h), pygame.RESIZABLE)

        pygame.display.set_caption("BDM FINAL STUDY")
        self.clock = pygame.time.Clock()
        self.rebuild_layout()

        self.db = DBManager()
        self.bank_def = self.load_questions("bdm_definitions.json", "def")
        self.bank_sql = self.load_questions("bdm_sql.json", "sql")
        self.all_questions = self.bank_def + self.bank_sql

        # Slide image cache
        self.slides_dir = "slides"
        self.slide_cache = {}

        self.state = "LANDING"
        self.mode: Optional[str] = None
        self.deck: List[Question] = []
        self.current_q: Optional[Question] = None
        self.current_options: List[str] = []
        self.current_correct_idx: Union[int, List[int], None] = None
        self.selected_options = set()
        self.correct = False
        self.score = 0
        self.total_unique_in_session = 0
        self.session_analytics = {}

        self.boss_hp_max = 10
        self.boss_hp = 10
        self.boss_kills = 0

        self.time_limit = 30.0
        self.question_start_time = 0
        self.use_timer = True

        self.scroll_y = 0
        self.click_zones = []

    # ------------------------------------------------------------------
    def rebuild_layout(self):
        w, h = self.screen.get_size()
        self.SCREEN_W, self.SCREEN_H = w, h

        # Anything narrower than 760 px gets the mobile single-column layout.
        # No sidebar, larger tap targets, scaled fonts.
        self.is_narrow = w < 760

        if self.is_narrow:
            # Scale UI elements based on actual screen width so phones of
            # different sizes (compact 360 px to large 480 px) all look right.
            scale = max(0.85, min(1.4, w / 400.0))
            self.margin = int(16 * scale)
            self.sidebar_w = 0
            self.sidebar_x = w
            self.content_w = max(260, w - (self.margin * 2))

            base   = int(15 * scale)
            small  = int(12 * scale)
            header = int(22 * scale)
            sub    = int(17 * scale)
            self.btn_h  = int(56 * scale)   # bigger tap targets on phone
            self.line_h = int(21 * scale)
        else:
            self.margin = 50
            self.sidebar_w = 350
            self.sidebar_x = w - self.sidebar_w
            self.content_w = max(280, w - self.sidebar_w - (self.margin * 2))
            base, small, header, sub = 18, 14, 32, 22
            self.btn_h, self.line_h = 60, 26

        self.font_main   = pygame.font.SysFont("Consolas", base)
        self.font_header = pygame.font.SysFont("Consolas", header, bold=True)
        self.font_sub    = pygame.font.SysFont("Consolas", sub, bold=True)
        self.font_small  = pygame.font.SysFont("Consolas", small)
        self.font_tiny   = pygame.font.SysFont("Consolas", max(10, int(small * 0.85)))

    # ------------------------------------------------------------------
    def load_questions(self, path, bank):
        if not os.path.exists(path):
            print(f"WARNING: {path} not found — skipping bank '{bank}'.")
            return []
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for q in data:
            obj = Question(**q)
            obj.bank = bank
            out.append(obj)
        return out

    # ------------------------------------------------------------------
    # Slide image support
    # ------------------------------------------------------------------
    def parse_slide_ref(self, ref):
        m = re.findall(r'Ch(\d+)\s*Slides?\s*([\d,\s]+)', ref or "")
        results = []
        for chap, nums in m:
            for n in re.findall(r'\d+', nums):
                results.append((int(chap), int(n)))
        return results

    def get_slide_image(self, ref):
        slides = self.parse_slide_ref(ref)
        if not slides:
            return None
        chap, num = slides[0]
        key = (chap, num)
        if key in self.slide_cache:
            return self.slide_cache[key]
        path = os.path.join(self.slides_dir, f"ch{chap:02d}_{num:02d}.png")
        if os.path.exists(path):
            try:
                img = pygame.image.load(path).convert()
                self.slide_cache[key] = img
                return img
            except Exception as e:
                print(f"Failed to load slide {path}: {e}")
                self.slide_cache[key] = None
                return None
        self.slide_cache[key] = None
        return None

    # ------------------------------------------------------------------
    # Drawing helpers
    # ------------------------------------------------------------------
    def wrap_text(self, text, font, max_w):
        out = []
        for raw_line in str(text).split("\n"):
            words = raw_line.split(" ")
            cur = []
            for word in words:
                trial = " ".join(cur + [word])
                if font.size(trial)[0] <= max_w:
                    cur.append(word)
                else:
                    if cur:
                        out.append(" ".join(cur))
                    cur = [word]
            out.append(" ".join(cur))
        return out

    def draw_text(self, text, x, y, color=C.TEXT, width=None, font=None, line_h=None, center=False):
        font = font or self.font_main
        width = width or self.content_w
        line_h = line_h or self.line_h
        lines = self.wrap_text(text, font, width)
        for i, line in enumerate(lines):
            surf = font.render(line, True, color)
            if center:
                rect = surf.get_rect(centerx=x + width // 2, top=y + i * line_h)
                self.screen.blit(surf, rect)
            else:
                self.screen.blit(surf, (x, y + i * line_h))
        return len(lines) * line_h

    def draw_button(self, text, x, y, w, h, action, selected=False, color_override=None):
        rect = pygame.Rect(int(x), int(y), int(w), int(h))
        mouse = pygame.mouse.get_pos()
        hov = rect.collidepoint(mouse)
        if color_override:
            bg = color_override
        elif selected:
            bg = C.BTN_SEL_HOV if hov else C.BTN_SEL_BG
        else:
            bg = C.BTN_HOV if hov else C.BTN_BG
        pygame.draw.rect(self.screen, bg, rect, border_radius=10)
        if selected:
            pygame.draw.rect(self.screen, C.GREEN, rect, width=2, border_radius=10)
        # Use the same font size as the body text on this device
        line_pitch = self.font_main.get_height() + 2
        lines = self.wrap_text(text, self.font_main, max(20, w - 28))
        max_lines = max(1, h // line_pitch)
        lines = lines[:max_lines]
        total_h = len(lines) * line_pitch
        sy = y + (h // 2) - (total_h // 2)
        for i, line in enumerate(lines):
            self.screen.blit(self.font_main.render(line, True, (255, 255, 255)),
                             (x + 14, sy + i * line_pitch))
        self.click_zones.append((rect, action))

    def mode_color(self):
        return {"LEARN": C.BLUE, "DRILL": C.GREEN, "BOSS": C.PURPLE}.get(self.mode, C.TEXT_DIM)

    # ------------------------------------------------------------------
    # Click / key dispatch
    # ------------------------------------------------------------------
    def handle_click(self, action):
        if action.startswith("MODE_"):
            self.mode = action.replace("MODE_", "")
            self.state = "BANK_MENU"
        elif action == "GUIDE":
            self.scroll_y = 0
            self.state = "STUDY_GUIDE"
        elif action == "STATS":
            self.state = "STATS"
        elif action == "RESET_PROGRESS":
            self.db.reset_progress()
            self.state = "LANDING"
        elif action == "HOME":
            self.scroll_y = 0
            self.state = "LANDING"
        elif action.startswith("BANK_"):
            self.start_session(action.replace("BANK_", ""))
        elif action.startswith("ANS_"):
            idx = int(action.replace("ANS_", ""))
            if self.current_q and self.current_q.type == "multi-select":
                if idx in self.selected_options:
                    self.selected_options.remove(idx)
                else:
                    self.selected_options.add(idx)
            else:
                self.check_answer(idx)
        elif action == "SUBMIT_MULTI":
            self.check_answer(-1)
        elif action == "NEXT":
            self.load_next_question()
        elif action == "QUIZ_ME":
            self.question_start_time = pygame.time.get_ticks()
            self.state = "QUESTION"
        elif action == "QUIT_SESSION":
            self.state = "SUMMARY"

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------
    def start_session(self, bank_filter):
        self.scroll_y = 0
        self.session_analytics = {}
        self.score = 0
        if bank_filter == "all":
            pool = list(self.all_questions)
        elif bank_filter == "def":
            pool = list(self.bank_def)
        elif bank_filter == "sql":
            pool = list(self.bank_sql)
        else:
            pool = list(self.all_questions)

        if self.mode == "DRILL":
            cur = self.db.conn.cursor()
            cur.execute("SELECT question_id FROM progress WHERE next_review > ?", (time.time(),))
            cooldown = {row[0] for row in cur.fetchall()}
            pool = [q for q in pool if q.id not in cooldown]

        if not pool:
            self.state = "SUMMARY"
            return

        random.shuffle(pool)

        if self.mode == "BOSS":
            pool = pool[:25]
            self.boss_hp = self.boss_hp_max
            self.boss_kills = 0

        self.deck = pool
        self.total_unique_in_session = len(self.deck)
        self.use_timer = self.mode in ("DRILL", "BOSS")
        self.load_next_question()

    def load_next_question(self):
        self.scroll_y = 0
        if not self.deck or (self.mode == "BOSS" and self.boss_hp <= 0):
            self.state = "SUMMARY"
            return
        self.current_q = self.deck.pop(0)
        self.question_start_time = pygame.time.get_ticks()

        if self.mode == "BOSS":
            base = 35.0 if self.current_q.type == "multi-select" else 18.0
        else:
            base = 45.0 if self.current_q.type == "multi-select" else 25.0
        self.time_limit = base

        if self.current_q.type == "tf":
            self.current_options = list(self.current_q.options)
        else:
            self.current_options = list(self.current_q.options)
            random.shuffle(self.current_options)
        self.selected_options.clear()

        if self.current_q.type == "multi-select":
            correct_strs = [self.current_q.options[i] for i in self.current_q.answer]
            self.current_correct_idx = [self.current_options.index(s) for s in correct_strs]
        else:
            correct_str = self.current_q.options[self.current_q.answer]
            self.current_correct_idx = self.current_options.index(correct_str)

        # In LEARN mode, show the source slide first if we have an image.
        if self.mode == "LEARN" and self.get_slide_image(self.current_q.slideRef) is not None:
            self.state = "STUDY_SLIDE"
        else:
            self.state = "QUESTION"

    def check_answer(self, user_val):
        self.scroll_y = 0
        if not self.current_q:
            return
        if self.current_q.type == "multi-select":
            self.correct = set(self.selected_options) == set(self.current_correct_idx)
        else:
            self.correct = user_val == self.current_correct_idx
        time_taken = (pygame.time.get_ticks() - self.question_start_time) / 1000.0
        if self.use_timer and time_taken > self.time_limit:
            self.correct = False
        topic = self.current_q.topic_group
        self.session_analytics.setdefault(topic, {"correct": 0, "wrong": 0, "time": 0.0, "attempts": 0})
        self.session_analytics[topic]["attempts"] += 1
        self.session_analytics[topic]["time"] += time_taken
        if self.mode in ("DRILL", "BOSS"):
            self.db.update_progress(self.current_q.id, topic, self.correct)
        if self.correct:
            self.score += 1
            self.session_analytics[topic]["correct"] += 1
            if self.mode == "BOSS":
                self.boss_kills += 1
        else:
            self.session_analytics[topic]["wrong"] += 1
            if self.mode in ("DRILL", "LEARN"):
                self.deck.append(self.current_q)
            elif self.mode == "BOSS":
                self.boss_hp -= 1
                self.deck.append(self.current_q)
        self.state = "FEEDBACK"

    # ------------------------------------------------------------------
    def apply_scroll_limits(self):
        max_scroll = 0
        if self.state == "STUDY_GUIDE":
            min_scroll = min(0, -(len(self.all_questions) * 140) + self.SCREEN_H - 100)
        elif self.state == "QUESTION":
            n_opts = len(self.current_options)
            content_h = 280 + n_opts * (self.btn_h + 12) + 160
            min_scroll = min(0, self.SCREEN_H - content_h)
        elif self.state == "FEEDBACK":
            min_scroll = -800
        else:
            min_scroll = 0
        self.scroll_y = max(min(self.scroll_y, max_scroll), min_scroll)

    # ------------------------------------------------------------------
    # Sidebar (desktop only)
    # ------------------------------------------------------------------
    def draw_sidebar(self):
        if self.is_narrow:
            return
        x = self.sidebar_x
        pygame.draw.rect(self.screen, C.PANEL, (x, 0, self.sidebar_w, self.SCREEN_H))
        pygame.draw.line(self.screen, C.PANEL_LINE, (x, 0), (x, self.SCREEN_H), 2)
        self.draw_text(f"MODE: {self.mode or '-'}", x + 30, 30, self.mode_color(), width=290, font=self.font_sub)
        self.draw_text(f"Score: {self.score} / {self.total_unique_in_session}", x + 30, 72, C.TEXT, width=290)

        if self.mode == "BOSS":
            self.draw_text("BOSS HP", x + 30, 110, C.PURPLE, width=290, font=self.font_small)
            for i in range(self.boss_hp_max):
                color = C.RED if i < self.boss_hp else (60, 30, 30)
                pygame.draw.rect(self.screen, color, (x + 30 + i * 26, 130, 22, 22), border_radius=3)
            wheel_y = 200
        else:
            wheel_y = 130

        self.draw_text("WEAKEST TOPICS", x + 30, wheel_y, C.YELLOW, width=290, font=self.font_small)
        stats = self.db.get_topic_accuracy()
        if not stats:
            self.draw_text("No data yet.", x + 30, wheel_y + 30, C.TEXT_FAINT, width=290, font=self.font_small)
        else:
            items = sorted(stats.items(), key=lambda kv: kv[1][0])[:8]
            y = wheel_y + 30
            for topic, (acc, n) in items:
                color = C.RED if acc < .5 else (C.YELLOW if acc < .8 else C.GREEN)
                self.draw_text(f"{int(acc*100):3d}% {topic[:22]}", x + 30, y, color, width=290, font=self.font_tiny, line_h=16)
                y += 22

    # ------------------------------------------------------------------
    # Mini stats strip (mobile, replaces sidebar)
    # ------------------------------------------------------------------
    def draw_mobile_stats_strip(self, y):
        """Show score/HP at a fixed y on mobile, inline above the question."""
        x = self.margin
        if self.mode == "BOSS":
            self.draw_text(f"Score {self.score}/{self.total_unique_in_session}",
                           x, y, C.TEXT, width=self.content_w // 2, font=self.font_small)
            # Mini HP bar
            for i in range(self.boss_hp_max):
                color = C.RED if i < self.boss_hp else (60, 30, 30)
                pygame.draw.rect(self.screen, color,
                                 (x + self.content_w - (self.boss_hp_max * 16), y + 2,
                                  14, 14), border_radius=2)
        else:
            self.draw_text(f"Score {self.score}/{self.total_unique_in_session}",
                           x, y, C.TEXT, width=self.content_w, font=self.font_small)

    # ------------------------------------------------------------------
    # Main UI dispatch
    # ------------------------------------------------------------------
    def draw_ui(self):
        self.click_zones.clear()
        if self.state == "LANDING": self.draw_landing()
        elif self.state == "BANK_MENU": self.draw_bank_menu()
        elif self.state == "STUDY_SLIDE": self.draw_study_slide()
        elif self.state == "QUESTION": self.draw_question()
        elif self.state == "FEEDBACK": self.draw_feedback()
        elif self.state == "STUDY_GUIDE": self.draw_study_guide()
        elif self.state == "STATS": self.draw_stats()
        elif self.state == "SUMMARY": self.draw_summary()
        if self.state in ("QUESTION", "FEEDBACK", "STUDY_SLIDE"):
            self.draw_sidebar()

    # ------------------------------------------------------------------
    def draw_landing(self):
        x = self.margin
        y = 60 if self.is_narrow else 85
        self.draw_text("BDM FINAL STUDY", x, y, C.GREEN, width=self.content_w, font=self.font_header)
        y += int(self.font_header.get_height() * 1.2)

        slide_count = 0
        if os.path.isdir(self.slides_dir):
            slide_count = len([f for f in os.listdir(self.slides_dir) if f.endswith('.png')])
        slide_str = f", {slide_count} slide images" if slide_count else " (no slides folder)"
        self.draw_text(
            f"Loaded: {len(self.bank_def)} concepts, {len(self.bank_sql)} SQL{slide_str}",
            x, y, C.TEXT_DIM, width=self.content_w, font=self.font_small,
        )
        y += int(self.font_small.get_height() * 2.5)

        bw = self.content_w
        modes = [
            ("[1] LEARN — see the slide, then quiz yourself (no timer)", "MODE_LEARN"),
            ("[2] DRILL — timed + spaced repetition", "MODE_DRILL"),
            ("[3] BOSS — 25-question HP gauntlet", "MODE_BOSS"),
        ]
        for label, action in modes:
            self.draw_button(label, x, y, bw, self.btn_h, action)
            y += self.btn_h + 12

        y += 10
        if self.is_narrow:
            # Stack vertically on phone
            self.draw_button("Study Guide", x, y, bw, self.btn_h, "GUIDE")
            y += self.btn_h + 8
            self.draw_button("Stats / Reset", x, y, bw, self.btn_h, "STATS")
            y += self.btn_h + 8
        else:
            half = (bw - 20) // 2
            self.draw_button("Study Guide", x, y, half, 50, "GUIDE")
            self.draw_button("Stats / Reset", x + half + 20, y, half, 50, "STATS")
            y += 70

        if not self.is_narrow:
            tip = "How to use: LEARN encodes new material. DRILL is retrieval practice. BOSS is the dress rehearsal."
            self.draw_text(tip, x, y, C.TEXT_FAINT, width=self.content_w, font=self.font_small)

    # ------------------------------------------------------------------
    def draw_bank_menu(self):
        x = self.margin
        y = 80 if not self.is_narrow else 50
        self.draw_text(f"{self.mode} MODE", x, y, self.mode_color(), width=self.content_w, font=self.font_header)
        y += int(self.font_header.get_height() * 1.2)
        self.draw_text("Pick a Bank", x, y, C.TEXT_DIM, width=self.content_w, font=self.font_small)
        y += int(self.font_small.get_height() * 2)

        bh = max(70, self.btn_h + 20)
        banks = [
            (f"[1] Definitions / Concepts ({len(self.bank_def)})", "BANK_def"),
            (f"[2] SQL Queries ({len(self.bank_sql)})", "BANK_sql"),
            (f"[3] Mixed ({len(self.all_questions)})", "BANK_all"),
        ]
        for label, action in banks:
            self.draw_button(label, x, y, self.content_w, bh, action)
            y += bh + 14

        self.draw_button("<- Back", x, self.SCREEN_H - self.btn_h - 20,
                         min(220, self.content_w), self.btn_h, "HOME")

    # ------------------------------------------------------------------
    # STUDY_SLIDE — show the source slide before the question (LEARN mode)
    # ------------------------------------------------------------------
    def draw_study_slide(self):
        q = self.current_q
        x = self.margin
        header_h = 90 if not self.is_narrow else 75
        pygame.draw.rect(self.screen, C.BG, (0, 0, self.sidebar_x if not self.is_narrow else self.SCREEN_W, header_h))
        self.draw_text("STUDY THE SLIDE — then quiz yourself", x, 12, C.BLUE,
                       width=self.content_w, font=self.font_small)
        self.draw_text(f"Source: {q.slideRef}", x, 12 + self.font_small.get_height() + 4,
                       C.YELLOW, width=self.content_w, font=self.font_small)
        self.draw_text(f"Q{q.id} | {q.topic_group} | Left: {len(self.deck)+1}",
                       x, 12 + (self.font_small.get_height() + 4) * 2,
                       C.GREEN, width=self.content_w, font=self.font_small)
        quit_y = 12
        self.draw_button("Quit", (self.sidebar_x if not self.is_narrow else self.SCREEN_W) - 84,
                         quit_y, 70, 32, "QUIT_SESSION")

        img = self.get_slide_image(q.slideRef)
        if img is None:
            self.draw_text("(Slide image not found)", x, header_h + 30, C.RED,
                           width=self.content_w)
            self.draw_button("Quiz me ->  (Space / Enter)", x, header_h + 80,
                             min(360, self.content_w), self.btn_h, "QUIZ_ME")
            return

        # Compute available space for the slide
        button_h = self.btn_h
        button_margin = 16
        avail_top = header_h + 16
        avail_bottom = self.SCREEN_H - button_h - button_margin - 16
        avail_w = self.content_w
        avail_h = avail_bottom - avail_top
        img_w, img_h = img.get_size()
        scale = min(avail_w / img_w, avail_h / img_h)
        new_w = int(img_w * scale)
        new_h = int(img_h * scale)
        scaled = pygame.transform.smoothscale(img, (new_w, new_h))
        ix = x + (avail_w - new_w) // 2
        iy = avail_top
        pygame.draw.rect(self.screen, C.PANEL_LINE, (ix - 2, iy - 2, new_w + 4, new_h + 4), border_radius=4)
        self.screen.blit(scaled, (ix, iy))

        btn_y = iy + new_h + button_margin
        self.draw_button("Quiz me ->  (Space / Enter)", x, btn_y,
                         min(360, self.content_w), button_h - 6, "QUIZ_ME",
                         color_override=(40, 100, 70))

    # ------------------------------------------------------------------
    def draw_question(self):
        q = self.current_q
        x = self.margin
        header_h = 140 if not self.is_narrow else 130
        sy = header_h + 25 + self.scroll_y
        text_h = self.draw_text(q.prompt, x, sy, C.TEXT, width=self.content_w, font=self.font_main)
        y = sy + text_h + 28
        for i, opt in enumerate(self.current_options):
            selected = i in self.selected_options
            # Compute button height based on wrapped text length so content fits
            wrapped = self.wrap_text(f"{i+1}. {opt}", self.font_main,
                                     max(20, self.content_w - 28))
            line_pitch = self.font_main.get_height() + 2
            needed = len(wrapped) * line_pitch + 24  # padding
            this_btn_h = max(self.btn_h, needed)
            self.draw_button(f"{i+1}. {opt}", x, y, self.content_w, this_btn_h,
                             f"ANS_{i}", selected=selected)
            y += this_btn_h + 12
        if q.type == "multi-select":
            self.draw_button("SUBMIT (Enter)", x, y + 8, min(400, self.content_w), self.btn_h, "SUBMIT_MULTI")

        # Pinned header
        pygame.draw.rect(self.screen, C.BG,
                         (0, 0, self.sidebar_x if not self.is_narrow else self.SCREEN_W, header_h))

        if self.use_timer:
            elapsed = (pygame.time.get_ticks() - self.question_start_time) / 1000.0
            pct = max(0, 1 - elapsed / self.time_limit)
            tcolor = C.GREEN if pct > .4 else C.RED
            pygame.draw.rect(self.screen, (60, 60, 60), (x, 14, self.content_w, 8))
            pygame.draw.rect(self.screen, tcolor, (x, 14, int(self.content_w * pct), 8))
        else:
            self.draw_text("LEARN MODE — no timer", x, 12, self.mode_color(),
                           width=self.content_w, font=self.font_small)

        line2_y = 32
        badge = {"mc": "MULTIPLE CHOICE", "tf": "TRUE / FALSE",
                 "multi-select": "SELECT ALL"}.get(q.type, q.type.upper())
        self.draw_text(badge, x, line2_y, C.BLUE, width=self.content_w, font=self.font_small)
        line3_y = line2_y + self.font_small.get_height() + 4
        header = f"Q{q.id} | {q.topic_group} | Diff {q.difficulty}/3 | Left: {len(self.deck)+1}"
        self.draw_text(header, x, line3_y, C.GREEN, width=self.content_w, font=self.font_small)
        line4_y = line3_y + self.font_small.get_height() + 2
        self.draw_text(f"Source: {q.slideRef}", x, line4_y, C.YELLOW,
                       width=self.content_w, font=self.font_small)

        # Mobile inline score (since no sidebar)
        if self.is_narrow:
            self.draw_mobile_stats_strip(line4_y + self.font_small.get_height() + 4)

        self.draw_button("Quit", (self.sidebar_x if not self.is_narrow else self.SCREEN_W) - 84,
                         line2_y - 4, 70, 32, "QUIT_SESSION")

    # ------------------------------------------------------------------
    def draw_feedback(self):
        q = self.current_q
        x = self.margin
        sy = 140 + self.scroll_y

        # Slide reference box — first thing you see
        ref_box_h = 50
        ref_color = C.YELLOW if self.correct else C.ORANGE
        pygame.draw.rect(self.screen, (35, 30, 20), (x, sy, self.content_w, ref_box_h), border_radius=6)
        pygame.draw.rect(self.screen, ref_color, (x, sy, self.content_w, ref_box_h), width=2, border_radius=6)
        ref_label = "REVIEW THIS SLIDE:" if not self.correct else "Source slide:"
        self.draw_text(ref_label, x + 14, sy + 6, ref_color,
                       width=self.content_w - 28, font=self.font_small)
        self.draw_text(q.slideRef, x + 14, sy + 24, C.TEXT,
                       width=self.content_w - 28, font=self.font_main)
        sy += ref_box_h + 20

        h = self.draw_text(q.explanation or "No explanation provided.", x, sy, C.TEXT, width=self.content_w)
        y = sy + h + 25
        if not self.correct:
            if q.type == "multi-select":
                y += self.draw_text("Correct answers:", x, y, C.GREEN, width=self.content_w)
                for idx in self.current_correct_idx:
                    y += self.draw_text(f"- {self.current_options[idx]}", x + 15, y, C.GREEN, width=self.content_w - 15)
            else:
                y += self.draw_text(f"Correct answer: {self.current_options[self.current_correct_idx]}",
                                    x, y, C.GREEN, width=self.content_w)
            y += 18
        y += self.draw_text(f"Topic: {q.topic}", x, y, C.TEXT_DIM, width=self.content_w, font=self.font_small)
        self.draw_button("Continue ->  (Enter / Space)", x, y + 25,
                         min(430, self.content_w), self.btn_h, "NEXT")

        # Pinned header
        pygame.draw.rect(self.screen, C.BG,
                         (0, 0, self.sidebar_x if not self.is_narrow else self.SCREEN_W, 125))
        elapsed = (pygame.time.get_ticks() - self.question_start_time) / 1000.0
        if self.correct:
            msg, color = "CORRECT!", C.GREEN
        elif self.use_timer and elapsed > self.time_limit:
            msg, color = "TIME'S UP — counted as miss", C.RED
        else:
            msg, color = "INCORRECT — this card will repeat", C.RED
        self.draw_text(msg, x, 62, color, width=self.content_w, font=self.font_header)

    # ------------------------------------------------------------------
    def _sort_key_by_slide(self, q):
        m = re.match(r'Ch(\d+)\s*Slides?\s*(\d+)', q.slideRef)
        if m:
            return (int(m.group(1)), int(m.group(2)), q.id)
        return (99, 999, q.id)

    def draw_study_guide(self):
        pygame.draw.rect(self.screen, C.BG, (0, 0, self.SCREEN_W, 70))
        self.draw_button("<- Back", self.margin, 15, 120, 38, "HOME")
        title = "STUDY GUIDE (sorted by slide)" if not self.is_narrow else "STUDY GUIDE"
        self.draw_text(title, self.margin + 140, 22, C.YELLOW,
                       width=self.content_w - 140, font=self.font_sub)
        y = 90 + self.scroll_y
        sorted_qs = sorted(self.all_questions, key=self._sort_key_by_slide)
        last_chapter = None
        for q in sorted_qs:
            if -160 < y < self.SCREEN_H + 100:
                m = re.match(r'Ch(\d+)', q.slideRef)
                this_chapter = m.group(1) if m else "?"
                if this_chapter != last_chapter:
                    y += 8
                    y += self.draw_text(f"━━━ CHAPTER {this_chapter} ━━━", self.margin, y, C.GREEN,
                                        width=self.content_w, font=self.font_sub, line_h=28)
                    y += 8
                    last_chapter = this_chapter
                color = C.BLUE if q.bank == "def" else C.PURPLE
                label = "[CONCEPT]" if q.bank == "def" else "[SQL]"
                y += self.draw_text(f"{q.slideRef}  {label}", self.margin, y, color,
                                    width=self.content_w, font=self.font_small)
                y += self.draw_text(q.prompt, self.margin, y, C.TEXT,
                                    width=self.content_w, font=self.font_small, line_h=20)
                if q.type == "multi-select":
                    answers = [q.options[i] for i in q.answer]
                else:
                    answers = [q.options[q.answer]]
                y += self.draw_text("Answer: " + ", ".join(answers), self.margin, y, C.GREEN,
                                    width=self.content_w, font=self.font_small, line_h=20)
                if q.explanation:
                    y += self.draw_text(q.explanation, self.margin, y, C.TEXT_DIM,
                                        width=self.content_w, font=self.font_small, line_h=20)
                y += 24
            else:
                m = re.match(r'Ch(\d+)', q.slideRef)
                this_chapter = m.group(1) if m else "?"
                if this_chapter != last_chapter:
                    last_chapter = this_chapter
                    y += 50
                y += 140

    def draw_stats(self):
        x = self.margin
        pygame.draw.rect(self.screen, C.BG, (0, 0, self.SCREEN_W, 70))
        self.draw_button("<- Back", x, 15, 120, 38, "HOME")
        self.draw_text("STATISTICS", x + 140, 22, C.YELLOW, width=self.content_w - 140, font=self.font_sub)
        stats = self.db.get_topic_accuracy()
        y = 100
        if not stats:
            self.draw_text("No data yet. Run a session first.", x, y, C.TEXT_DIM, width=self.content_w)
        else:
            self.draw_text("Per-topic accuracy, weakest first:", x, y, C.YELLOW, width=self.content_w)
            y += 36
            for topic, (acc, n) in sorted(stats.items(), key=lambda kv: kv[1][0]):
                color = C.RED if acc < .5 else (C.YELLOW if acc < .8 else C.GREEN)
                self.draw_text(f"{int(acc*100):3d}% ({n:2d})  {topic}", x, y, color,
                               width=self.content_w, font=self.font_small)
                y += 26
                if y > self.SCREEN_H - 95:
                    break
        self.draw_button("Reset all progress", x, self.SCREEN_H - self.btn_h - 20,
                         min(400, self.content_w), self.btn_h, "RESET_PROGRESS",
                         color_override=(120, 50, 50))

    def draw_summary(self):
        x = self.margin
        y = 60 if self.is_narrow else 80
        self.draw_text("SESSION COMPLETE", x, y, C.GREEN, width=self.content_w, font=self.font_header)
        y += int(self.font_header.get_height() * 1.2)
        if self.mode == "BOSS":
            msg = "DEFEATED — HP hit 0" if self.boss_hp <= 0 else f"VICTORY — HP remaining: {self.boss_hp}"
            self.draw_text(msg, x, y, C.RED if self.boss_hp <= 0 else C.GREEN,
                           width=self.content_w, font=self.font_sub)
            y += int(self.font_sub.get_height() * 1.2)
            self.draw_text(f"Questions defeated: {self.boss_kills}", x, y, C.TEXT, width=self.content_w)
            y += 30
        if not self.session_analytics:
            self.draw_text("No questions answered.", x, y, C.TEXT_DIM, width=self.content_w)
            y += 40
        else:
            self.draw_text("Per-topic breakdown:", x, y, C.YELLOW, width=self.content_w, font=self.font_sub)
            y += 38
            for topic, d in sorted(self.session_analytics.items()):
                acc = d["correct"] / d["attempts"] * 100 if d["attempts"] else 0
                avgt = d["time"] / d["attempts"] if d["attempts"] else 0
                color = C.RED if acc < 50 else (C.YELLOW if acc < 80 else C.GREEN)
                self.draw_text(f"{acc:5.1f}% | Misses {d['wrong']:2d} | Avg {avgt:4.1f}s | {topic}",
                               x, y, color, width=self.content_w, font=self.font_small)
                y += 25
                if y > self.SCREEN_H - 115:
                    break
        self.draw_button("Main Menu", x, self.SCREEN_H - self.btn_h - 20,
                         min(400, self.content_w), self.btn_h, "HOME")

    # ------------------------------------------------------------------
    def run(self):
        running = True
        while running:
            self.screen.fill(C.BG)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.VIDEORESIZE:
                    flags = self.screen.get_flags()
                    self.screen = pygame.display.set_mode((event.w, event.h), flags)
                    self.rebuild_layout()
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    for rect, action in self.click_zones:
                        if rect.collidepoint(event.pos):
                            self.handle_click(action)
                            break
                elif event.type == pygame.FINGERDOWN:
                    # Touch on phone — convert normalized coords to pixel coords
                    px = int(event.x * self.SCREEN_W)
                    py = int(event.y * self.SCREEN_H)
                    for rect, action in self.click_zones:
                        if rect.collidepoint(px, py):
                            self.handle_click(action)
                            break
                elif event.type == pygame.MOUSEWHEEL:
                    if self.state in ("STUDY_GUIDE", "QUESTION", "FEEDBACK"):
                        self.scroll_y += event.y * 45
                        self.apply_scroll_limits()
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_DOWN and self.state in ("STUDY_GUIDE", "QUESTION", "FEEDBACK"):
                        self.scroll_y -= 60; self.apply_scroll_limits()
                    elif event.key == pygame.K_UP and self.state in ("STUDY_GUIDE", "QUESTION", "FEEDBACK"):
                        self.scroll_y += 60; self.apply_scroll_limits()
                    elif event.key == pygame.K_ESCAPE:
                        if self.state in ("BANK_MENU", "STUDY_GUIDE", "STATS"):
                            self.handle_click("HOME")
                        elif self.state == "QUESTION":
                            self.state = "SUMMARY"
                    elif self.state == "LANDING":
                        if   event.key == pygame.K_1: self.handle_click("MODE_LEARN")
                        elif event.key == pygame.K_2: self.handle_click("MODE_DRILL")
                        elif event.key == pygame.K_3: self.handle_click("MODE_BOSS")
                    elif self.state == "BANK_MENU":
                        if   event.key == pygame.K_1: self.handle_click("BANK_def")
                        elif event.key == pygame.K_2: self.handle_click("BANK_sql")
                        elif event.key == pygame.K_3: self.handle_click("BANK_all")
                    elif self.state == "QUESTION":
                        if pygame.K_1 <= event.key <= pygame.K_9:
                            idx = event.key - pygame.K_1
                            if idx < len(self.current_options):
                                self.handle_click(f"ANS_{idx}")
                        elif event.key == pygame.K_RETURN and self.current_q and self.current_q.type == "multi-select":
                            self.handle_click("SUBMIT_MULTI")
                    elif self.state == "FEEDBACK":
                        if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                            self.handle_click("NEXT")
                    elif self.state == "STUDY_SLIDE":
                        if event.key in (pygame.K_RETURN, pygame.K_SPACE):
                            self.handle_click("QUIZ_ME")

            if self.state == "QUESTION" and self.use_timer:
                elapsed = (pygame.time.get_ticks() - self.question_start_time) / 1000.0
                if elapsed >= self.time_limit:
                    self.check_answer(-1)

            self.draw_ui()
            pygame.display.flip()
            self.clock.tick(30)
        pygame.quit()
        sys.exit()


def main():
    parser = argparse.ArgumentParser(description="BDM Final Study")
    parser.add_argument("--mobile",  action="store_true", help="Force narrow phone layout (full screen on phones)")
    parser.add_argument("--desktop", action="store_true", help="Force desktop window (1200x800)")
    args = parser.parse_args()

    if args.mobile:
        force = "mobile"
    elif args.desktop:
        force = "desktop"
    else:
        force = None
    BDMFinalStudy(force_mode=force).run()


if __name__ == "__main__":
    main()
