"""Generate the fictional sample tender and bids used by the demo.

Run from backend/:  .venv/bin/python demo/make_samples.py [--out DIR]

Every name, tax ID (STIR), address, date and amount is invented. The output
is deterministic: fixed metadata dates, a fixed embedded font subset and
reproducible PDF saving. Bidder B's reference letter is a scanned image (no
text layer) so the OCR path gets exercised.

Layout: tender/, bidder-a/ (mostly compliant bid), bidder-b/ (planted
problems). The answer key is demo/samples/EXPECTED.md (written by hand).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "samples"
FONT_FILE = HERE / "fonts" / "DejaVuSans-subset.ttf"
FONT_NAME = "dvs"
FIXED_DATE = "D:20260101120000+05'00'"

PAGE_W = 595.0  # A4, points
PAGE_H = 842.0
MARGIN_X = 52.0
MARGIN_TOP = 50.0
MARGIN_BOTTOM = 62.0
CONTENT_W = PAGE_W - 2 * MARGIN_X
LINE = 1.42
FOOTER = "ДЕМО-ОБРАЗЕЦ: все названия, ИНН (СТИР), адреса, даты и суммы вымышленные"


class Writer:
    """Minimal top-to-bottom text layout on A4 pages, built on PyMuPDF."""

    def __init__(self) -> None:
        self.doc = pymupdf.open()
        self._font = pymupdf.Font(fontfile=str(FONT_FILE))
        self._text_pages: set[int] = set()
        self.page: pymupdf.Page
        self.y = MARGIN_TOP
        self._new_page()

    # -- pages -------------------------------------------------------------
    def _new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.page.insert_font(fontname=FONT_NAME, fontfile=str(FONT_FILE))
        self._text_pages.add(self.page.number)
        self.y = MARGIN_TOP

    def new_page(self) -> None:
        """Start a new page unless the current one is still empty."""
        if self.y > MARGIN_TOP + 1:
            self._new_page()

    def image_page(self, jpeg: bytes) -> None:
        """Append a full-page raster image with no text layer."""
        page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(page.rect, stream=jpeg)

    # -- text helpers ------------------------------------------------------
    def _width(self, text: str, size: float) -> float:
        return self._font.text_length(text, fontsize=size)

    def _wrap(self, text: str, size: float, width: float) -> list[str]:
        lines: list[str] = []
        for chunk in text.split("\n"):
            words = chunk.split()
            if not words:
                lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if self._width(candidate, size) <= width:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines

    def _draw(self, lines: list[str], x: float, top: float, size: float) -> None:
        y = top
        for line in lines:
            if line:
                self.page.insert_text(
                    (x, y + size * 0.95), line, fontname=FONT_NAME, fontsize=size
                )
            y += size * LINE

    def _emit(self, lines: list[str], x: float, size: float) -> None:
        """Write lines at the cursor, breaking pages as needed."""
        for line in lines:
            if self.y + size * LINE > PAGE_H - MARGIN_BOTTOM:
                self._new_page()
            self._draw([line], x, self.y, size)
            self.y += size * LINE

    # -- blocks ------------------------------------------------------------
    def title(self, text: str, size: float = 15, gap: float = 4) -> None:
        if self.y + size * LINE > PAGE_H - MARGIN_BOTTOM:
            self._new_page()
        width = self._width(text, size)
        self.page.insert_text(
            ((PAGE_W - width) / 2, self.y + size * 0.95),
            text,
            fontname=FONT_NAME,
            fontsize=size,
        )
        self.y += size * LINE + gap

    def center(self, text: str, size: float = 10, gap: float = 2) -> None:
        for line in self._wrap(text, size, CONTENT_W):
            width = self._width(line, size)
            self._emit_centered(line, (PAGE_W - width) / 2, size)
        self.y += gap

    def _emit_centered(self, line: str, x: float, size: float) -> None:
        if self.y + size * LINE > PAGE_H - MARGIN_BOTTOM:
            self._new_page()
        self._draw([line], x, self.y, size)
        self.y += size * LINE

    def heading(self, text: str, size: float = 12.5) -> None:
        self.y += 8
        lines = self._wrap(text, size, CONTENT_W)
        if self.y + len(lines) * size * LINE + 14 > PAGE_H - MARGIN_BOTTOM:
            self._new_page()
        self._emit(lines, MARGIN_X, size)
        self.page.draw_line(
            (MARGIN_X, self.y + 1),
            (PAGE_W - MARGIN_X, self.y + 1),
            color=(0.45, 0.45, 0.45),
            width=0.6,
        )
        self.y += 7

    def para(self, text: str, size: float = 10.4, indent: float = 0.0, gap: float = 5) -> None:
        self._emit(self._wrap(text, size, CONTENT_W - indent), MARGIN_X + indent, size)
        self.y += gap

    def bullet(self, text: str, marker: str = "•", size: float = 10.4, gap: float = 3.5) -> None:
        hang = 16.0
        lines = self._wrap(text, size, CONTENT_W - hang)
        if self.y + size * LINE > PAGE_H - MARGIN_BOTTOM:
            self._new_page()
        self._draw([marker], MARGIN_X, self.y, size)
        self._emit(lines, MARGIN_X + hang, size)
        self.y += gap

    def gap(self, height: float) -> None:
        self.y += height

    def kv_rows(self, rows: list[tuple[str, str]], widths: tuple[float, float] = (1.0, 2.2)) -> None:
        self.table(None, [list(r) for r in rows], widths)

    def table(
        self,
        header: list[str] | None,
        rows: list[list[str]],
        widths: tuple[float, ...] | list[float],
        size: float = 9.4,
        pad: float = 4.2,
    ) -> None:
        total = float(sum(widths))
        cols = [CONTENT_W * w / total for w in widths]

        def cell_lines(cells: list[str]) -> list[list[str]]:
            return [self._wrap(str(c), size, col - 2 * pad) for c, col in zip(cells, cols)]

        def draw_row(lines_per_cell: list[list[str]], is_header: bool) -> None:
            height = max(len(lines) for lines in lines_per_cell) * size * LINE + 2 * pad
            x = MARGIN_X
            for lines, col in zip(lines_per_cell, cols):
                self.page.draw_rect(
                    pymupdf.Rect(x, self.y, x + col, self.y + height),
                    color=(0.35, 0.35, 0.35),
                    fill=(0.9, 0.9, 0.9) if is_header else None,
                    width=0.5,
                )
                self._draw(lines, x + pad, self.y + pad, size)
                x += col
            self.y += height

        def row_height(lines_per_cell: list[list[str]]) -> float:
            return max(len(lines) for lines in lines_per_cell) * size * LINE + 2 * pad

        header_lines = cell_lines(header) if header else None
        body = [cell_lines(cells) for cells in rows]
        if header_lines is not None:
            # Keep the header with its first row so it is never stranded.
            needed = row_height(header_lines) + (row_height(body[0]) if body else 0)
            if self.y + needed > PAGE_H - MARGIN_BOTTOM:
                self._new_page()
            draw_row(header_lines, True)
        for lines_per_cell in body:
            if self.y + row_height(lines_per_cell) > PAGE_H - MARGIN_BOTTOM:
                self._new_page()
                if header_lines is not None:
                    draw_row(header_lines, True)
            draw_row(lines_per_cell, False)
        self.y += 7

    def signature(self, who: str) -> None:
        self.gap(16)
        self.para(f"{who}  ______________________  (подпись)        М.П.", gap=2)

    # -- output ------------------------------------------------------------
    def tobytes(self) -> bytes:
        total = self.doc.page_count
        for index in sorted(self._text_pages):
            page = self.doc[index]
            page.insert_text(
                (MARGIN_X, PAGE_H - 28), FOOTER, fontname=FONT_NAME, fontsize=6.8,
                color=(0.42, 0.42, 0.42),
            )
            label = f"стр. {index + 1} из {total}"
            page.insert_text(
                (PAGE_W - MARGIN_X - self._width(label, 6.8), PAGE_H - 28), label,
                fontname=FONT_NAME, fontsize=6.8, color=(0.42, 0.42, 0.42),
            )
        self.doc.set_metadata(
            {
                "title": "Demo sample (fictional)",
                "author": "make_samples.py",
                "subject": "Fictional tender sample for the demo, not a real document",
                "keywords": "demo, fictional, sample",
                "creator": "make_samples.py",
                "producer": "PyMuPDF",
                "creationDate": FIXED_DATE,
                "modDate": FIXED_DATE,
            }
        )
        return self.doc.tobytes(
            garbage=4, deflate=True, no_new_id=True, reproducible=True
        )


# ---------------------------------------------------------------------------
# Tender documentation (Uzbek Latin legal parts, Russian technical spec)
# ---------------------------------------------------------------------------

TENDER_SPEC_ROWS = [
    ["1", "Процессор", "не менее 8 ядер, базовая частота не менее 3,0 ГГц, или эквивалент", "—"],
    ["2", "Оперативная память (RAM)", "не менее 16 ГБ, тип DDR5, или эквивалент", "ГБ"],
    ["3", "Накопитель", "SSD, интерфейс NVMe, не менее 512 ГБ", "ГБ"],
    ["4", "Диагональ экрана", "15,6 дюйма, разрешение не менее 1920×1080", "дюйм"],
    ["5", "Видеоподсистема", "интегрированная, или эквивалент", "—"],
    ["6", "Аккумулятор", "не менее 45 Втч; время работы не менее 8 часов", "Втч, ч"],
    ["7", "Масса", "не более 2,0 кг", "кг"],
    ["8", "Операционная система", "Windows 11 Pro или эквивалент, лицензия на каждый ноутбук", "—"],
    ["9", "Гарантийный срок", "не менее 24 (двадцати четырёх) месяцев с даты поставки", "месяцев"],
    ["10", "Комплектация", "сумка-чехол, проводная мышь, блок питания", "шт."],
]


def build_tender() -> bytes:
    w = Writer()
    w.title("ТЕНДЕРНАЯ ДОКУМЕНТАЦИЯ", 15)
    w.title("TENDER HUJJATLARI", 11, gap=8)
    w.center("Поставка ноутбуков для учебного центра", 12.5, gap=1)
    w.center("Oʻquv markazi uchun noutbuklar yetkazib berish", 11, gap=1)
    w.center("Закупка № DEMO-2026-017 · Лот № 1 · 20 (двадцать) штук", 10, gap=10)
    w.para(
        "Образец для демонстрации. Заказчик, поставщики и все реквизиты вымышленные; "
        "такие организации не существуют.",
        size=8.8,
        gap=8,
    )

    w.heading("1. Umumiy maʻlumotlar / Общие сведения")
    w.kv_rows(
        [
            ("Номер закупки / Xarid raqami", "DEMO-2026-017"),
            ("Заказчик / Buyurtmachi", "ГУП «Пример Заказчик», г. Примерск, ул. Образцовая, д. 1"),
            ("Предмет закупки / Xarid predmeti", "Поставка ноутбуков / Noutbuklar yetkazib berish"),
            ("Лот / Lot", "Лот № 1: 20 (двадцать) штук ноутбуков, новые"),
            (
                "Начальная цена / Boshlangʻich narx",
                "186 000 000 (сто восемьдесят шесть миллионов) сум, с учётом НДС",
            ),
            (
                "Срок подачи заявок / Arizalarni topshirish muddati",
                "до 30.12.2026, 10:00 (время Примерска)",
            ),
            ("Вскрытие конвертов / Konvertlarni ochish", "30.12.2026, 11:00, электронная площадка Заказчика"),
            (
                "Срок действия предложения / Taklif amal qilish muddati",
                "не менее 90 (девяноста) календарных дней",
            ),
            ("Контакты / Aloqa", "tender@primer-zakazchik.example, тел. +998 00 000 00 00"),
        ]
    )

    w.heading("2. Texnik talablar / Технические требования к товару")
    w.para(
        "Yetkazib beriladigan noutbuklar quyidagi talablarga javob berishi shart. "
        "Ekvivalent taklif qilinishi mumkin, biroq u barcha talablarga javob berishi va "
        "texnik taqqoslash jadvali bilan tasdiqlanishi kerak (или эквивалент)."
    )
    w.table(["№", "Параметр", "Требование", "Ед. изм."], TENDER_SPEC_ROWS, (0.45, 2.0, 5.0, 1.1))
    w.para(
        "Все поставляемые ноутбуки должны быть новыми, не бывшими в эксплуатации, "
        "с серийными номерами производителя и заводской упаковкой."
    )

    w.heading("3. Yetkazib berish shartlari / Условия поставки")
    w.bullet("Срок поставки: не более 30 (тридцати) календарных дней с даты заключения договора.")
    w.bullet("Yetkazib berish manzili: г. Примерск, ул. Образцовая, д. 1, склад Заказчика.")
    w.bullet(
        "Tovar bilan birga komplektlilik va ishlash qobiliyatini tekshirish dalolatnomasi "
        "topshiriladi / Вместе с товаром передаётся акт проверки комплектности и работоспособности."
    )

    w.heading("4. Ishtirokchilarga qoʻyiladigan talablar / Требования к участникам")
    w.para("Tenderda ishtirok etish uchun ishtirokchi quyidagi hujjatlarni taqdim etishi shart:")
    w.bullet(
        "4.1. Davlat roʻyxatidan oʻtganlik guvohnomasi nusxasi / "
        "Копия свидетельства о государственной регистрации."
    )
    w.bullet(
        "4.2. Soliq organidan ma'lumotnoma (soliq qarzdorligi yoʻqligi) / Налоговая справка "
        "(отсутствие задолженности), выданная не ранее чем за 30 календарных дней до срока подачи "
        "заявок, то есть не ранее 30.11.2026."
    )
    w.bullet(
        "4.3. Lot boʻyicha tovarni yetkazib berish huquqini beruvchi litsenziya yoki ishlab "
        "chiqaruvchining (rasmiy dilerning) xati / Лицензия или дилерское письмо производителя "
        "на поставку товара, действующие на дату подачи заявки."
    )
    w.bullet(
        "4.4. 2024–2025 yillarda shunga oʻxshash kamida 3 (uch) ta shartnoma bajarilganligi, "
        "nusxalari va qabul dalolatnomalari bilan / Не менее 3 (трёх) аналогичных договоров "
        "поставки за 2024–2025 гг. с копиями договоров и актов приёмки."
    )
    w.bullet(
        "4.5. Ishtirokchining 1-shakl boʻyicha kafolat xati (Ilova № 1) / Гарантийное письмо "
        "участника по форме 1 (Приложение № 1)."
    )
    w.bullet("4.6. Ariza-xat (Ilova № 2) / Письмо-заявка по форме (Приложение № 2).")
    w.bullet("4.7. Taklif amal qilish muddati kamida 90 kun / Срок действия предложения не менее 90 дней.")

    w.heading("5. Baholash mezonlari / Критерии оценки")
    w.para(
        "Takliflar quyidagi mezonlar boʻyicha ball bilan baholanadi. Maksimal ball — 10. "
        "Oʻtish balli — 7 (yetti). / Заявки оцениваются по баллам. Максимальный балл — 10. "
        "Проходной балл — 7 (семь). Участники, набравшие менее 7 баллов, к дальнейшему "
        "рассмотрению не допускаются."
    )
    w.table(
        ["Критерий / Mezon", "Условие / Shart", "Баллы / Ball"],
        [
            [
                "Соответствие ТЗ / Texnik talablarga muvofiqlik",
                "Все параметры раздела 2 подтверждены в техническом предложении. "
                "Несоответствие обязательному параметру ведёт к отклонению заявки.",
                "4",
            ],
            [
                "Гарантийный срок / Kafolat muddati",
                "24 месяца — 2 балла; более 24 месяцев — 3 балла",
                "до 3",
            ],
            [
                "Опыт поставок / Tajriba",
                "3 и более договоров за 2024–2025 гг. — 2 балла; 1–2 договора — 1 балл",
                "до 2",
            ],
            [
                "Срок поставки / Yetkazib berish muddati",
                "не более 30 календарных дней — 1 балл",
                "1",
            ],
            ["Итого / Jami", "максимальная сумма баллов", "10"],
        ],
        (2.2, 4.4, 1.0),
    )

    w.heading("6. Shartnoma shartlari / Условия договора")
    w.bullet(
        "6.1. To'lov: tovar qabul qilinib, qabul dalolatnomasi imzolangandan keyin 30 (o'ttiz) bank "
        "kuni ichida 100 % miqdorda amalga oshiriladi / Оплата: 100 % в течение 30 банковских дней "
        "после подписания акта приёмки."
    )
    w.bullet(
        "6.2. Kafolat muddati: tovar qabul qilingan kundan boshlab 12 (oʻn ikki) oy / Гарантийный "
        "срок: 12 (двенадцать) месяцев с даты приёмки товара."
    )
    w.bullet(
        "6.3. Kechiktirilgan har bir kun uchun 0,1 % jarima, jami shartnoma summasining 10 % "
        "idan oshmasligi kerak / Пеня за каждый день просрочки 0,1 %, но не более 10 % от суммы договора."
    )
    w.bullet(
        "6.4. Shartnoma gʻolib aniqlangan kundan boshlab 10 (oʻn) kalendar kuni ichida imzolanadi / "
        "Договор подписывается в течение 10 календарных дней после определения победителя."
    )

    w.heading("7. Taklifning tarkibi / Состав заявки")
    w.bullet("Письмо-заявка (Приложение № 2).", marker="1)")
    w.bullet("Гарантийное письмо участника по форме 1 (Приложение № 1).", marker="2)")
    w.bullet("Техническое предложение с таблицей соответствия (Приложение № 3).", marker="3)")
    w.bullet("Документы раздела 4: регистрационные, налоговые, лицензия или дилерское письмо.", marker="4)")
    w.bullet("Копии договоров и актов приёмки за 2024–2025 гг.", marker="5)")
    w.bullet("Ценовое предложение (сумма с НДС).", marker="6)")

    w.heading("8. Qoʻshimcha shartlar / Прочие условия")
    w.bullet(
        "Tanlov komissiyasi qarorlari ustidan shikoyat O‘zbekiston Respublikasi qonunchiligida "
        "belgilangan tartibda beriladi / Обжалование решений тендерной комиссии осуществляется "
        "в порядке, установленном законодательством."
    )
    w.bullet(
        "Ushbu hujjat namunaviy (demonstratsion) maqsadda tayyorlangan. Barcha nomlar, STIR, manzillar, "
        "sanalar va summalar soxta / Настоящий документ подготовлен в демонстрационных целях; все "
        "названия, ИНН, адреса, даты и суммы вымышленные."
    )


    w.new_page()
    w.heading("9. Taqdim etish tartibi / Порядок подачи заявок")
    w.bullet(
        "Takliflar elektron xarid maydonchasi orqali yoki Zakazchiga shaxsan topshiriladi. Hujjatlar "
        "rus yoki oʻzbek tilida taqdim etiladi / Заявки подаются в электронном виде на площадку "
        "Заказчика либо лично по адресу Заказчика, на русском или узбекском языке."
    )
    w.bullet(
        "Narxlar soʻmda koʻrsatiladi, NDS alohida koʻrsatiladi / Цены указываются в сумах; НДС "
        "указывается отдельной строкой."
    )
    w.bullet(
        "Har bir hujjat nusxasi «Nusxa to‘g‘ri» belgisi va imzo bilan tasdiqlanadi / Каждая копия "
        "заверяется подписью руководителя и печатью участника."
    )
    w.bullet(
        "Zakazchi taklif muddati tugagunga qadar ishtirokchilardan takliflarni uzaytirishni so‘rashga "
        "haqli / Заказчик вправе запросить продление срока действия предложения до истечения срока."
    )

    w.heading("10. Xizmat koʻrsatish / Сервисное обслуживание")
    w.para(
        "Поставщик обеспечивает гарантийное обслуживание в течение всего гарантийного срока: "
        "замену неисправных комплектующих и ремонт по месту эксплуатации в срок не более 5 рабочих "
        "дней с момента обращения Заказчика. Контактное лицо поставщика и телефон указываются в "
        "письме-заявке."
    )
    w.para(
        "Tovar bilan birga foydalanish boʻyicha qoʻllanma va texnik xizmat koʻrsatish telefoni "
        "beriladi / Вместе с товаром передаются инструкция по эксплуатации и телефон сервисной службы."
    )

    w.heading("11. Tushuntirishlar / Разъяснения")
    w.para(
        "Tender hujjatlariga oid savollar tanlov arizalari topshirilishidan kamida 5 (besh) kun oldin "
        "yozma shaklda yuboriladi. Javoblar barcha ishtirokchilarga bir vaqtda yuboriladi / Вопросы "
        "к тендерной документации направляются в письменной форме не позднее чем за 5 календарных "
        "дней до срока подачи заявок. Ответы направляются всем участникам одновременно."
    )
    w.new_page()
    w.heading("Ilova № 1 / Приложение № 1. Форма 1: гарантийное письмо участника")
    w.para(
        "Biz, ________________________ (ishtirokchi nomi), tender № DEMO-2026-017 (Lot № 1) "
        "boʻyicha gʻolib deb topilsak, shartnomani belgilangan muddatda imzolashga va "
        "shartnoma taʼminotini taqdim etishga kafolat beramiz."
    )
    w.para(
        "Мы, ________________________ (наименование участника), подтверждаем, что в случае признания "
        "нас победителем тендера № DEMO-2026-017 (Лот № 1) мы подпишем договор в установленный срок и "
        "предоставим обеспечение исполнения договора."
    )
    w.para("Гарантийный взнос (обеспечение заявки): 1 860 000 (один миллион восемьсот шестьдесят тысяч) сум.")
    w.signature("Руководитель участника")

    w.heading("Ilova № 2 / Приложение № 2. Форма письма-заявки")
    w.para("Исх. № ______ от «___» ____________ 2026 г.")
    w.para("Заказчику: ГУП «Пример Заказчик», тендерная комиссия")
    w.para(
        "Просим допустить наше предприятие к участию в тендере № DEMO-2026-017 (Лот № 1). "
        "Предлагаем поставить 20 (двадцать) ноутбуков в соответствии с разделом 2 на сумму "
        "__________ сум, включая НДС; срок поставки ____ календарных дней; гарантийный срок ____ "
        "месяцев; срок действия предложения ____ календарных дней."
    )
    w.para("Мы принимаем условия тендерной документации и подтверждаем достоверность сведений.")
    w.signature("Руководитель участника")

    w.new_page()
    w.heading("Ilova № 3 / Приложение № 3. Таблица соответствия техническим требованиям")
    w.para("Заполняется участником по каждой позиции раздела 2 с указанием подтверждающего документа.")
    w.table(
        ["№", "Параметр", "Требование ТЗ", "Предложение участника", "Подтверждение"],
        [[row[0], row[1], row[2], "", ""] for row in TENDER_SPEC_ROWS],
        (0.45, 1.6, 3.0, 2.2, 1.6),
    )

    w.new_page()
    w.new_page()
    w.heading("Ilova № 4 / Приложение № 4. Спецификация лота № 1 (ориентировочная)")
    w.table(
        ["№", "Наименование", "Кол-во", "Ед.", "Цена за ед., сум", "Сумма, сум"],
        [
            ["1", "Ноутбук, соответствующий разделу 2 (или эквивалент)", "20", "шт.", "9 300 000", "186 000 000"],
            ["", "Итого с НДС", "20", "шт.", "—", "186 000 000"],
        ],
        (0.45, 3.0, 0.9, 0.7, 1.5, 1.6),
    )
    w.para("Цены ориентировочные и служат для расчёта начальной цены лота; участники указывают свои цены в письме-заявке.")

    w.new_page()
    w.heading("Ilova № 5 / Приложение № 5. Shartnoma loyihasi (ajratma) / Проект договора (извлечение)")
    w.bullet("1. Predmet / Предмет: поставка 20 (двадцати) ноутбуков в соответствии с разделом 2 тендерной документации.")
    w.bullet("2. Narx / Цена: договорная сумма определяется победившим участником, не выше начальной цены лота.")
    w.bullet("3. Yetkazib berish / Поставка: в срок, указанный победителем в письме-заявке, но не более 30 дней.")
    w.bullet("4. Kafolat / Гарантия: в соответствии с разделом 2 и пунктом 6 тендерной документации.")
    w.bullet("5. Javobgarlik / Ответственность: сторона, нарушившая сроки, уплачивает пеню согласно разделу 6.")
    w.bullet("6. Nizolar / Споры: разрешаются в хозяйственном суде по месту нахождения Заказчика.")
    return w.tobytes()


# ---------------------------------------------------------------------------
# Bidder A: ООО «Пример Техно», STIR 123456789 (mostly compliant)
# ---------------------------------------------------------------------------

TECH_PROPOSAL_A = [
    ["1", "Процессор", "не менее 8 ядер, не менее 3,0 ГГц, или эквивалент", "8 ядер, 3,4 ГГц (процессор ПРО-8, соответствует)"],
    ["2", "Оперативная память", "не менее 16 ГБ DDR5", "16 ГБ DDR5, 5600 МГц (соответствует)"],
    ["3", "Накопитель", "SSD NVMe не менее 512 ГБ", "512 ГБ SSD NVMe (соответствует)"],
    ["4", "Диагональ экрана", "15,6 дюйма, не менее 1920×1080", "15,6 дюйма, 1920×1080 IPS (соответствует)"],
    ["5", "Видеоподсистема", "интегрированная, или эквивалент", "интегрированная (соответствует)"],
    ["6", "Аккумулятор", "не менее 45 Втч; не менее 8 часов", "52 Втч; до 9 часов по паспорту (соответствует)"],
    ["7", "Масса", "не более 2,0 кг", "1,8 кг (соответствует)"],
    ["8", "Операционная система", "Windows 11 Pro или эквивалент", "Windows 11 Pro, лицензия на каждый ноутбук (соответствует)"],
    ["9", "Гарантийный срок", "не менее 24 месяцев", "36 (тридцать шесть) месяцев (соответствует)"],
    ["10", "Комплектация", "сумка-чехол, мышь, блок питания", "все позиции в наличии (соответствует)"],
]


def _bidder_a_cover(w: Writer) -> None:
    w.para("ООО «Пример Техно»\nСТИР 123456789 · г. Примерск, ул. Тестовая, д. 5\nИсх. № 118 от 28.12.2026", size=9.5, gap=10)
    w.para("Заказчику: ГУП «Пример Заказчик», тендерная комиссия", gap=12)
    w.title("ПИСЬМО-ЗАЯВКА / ARIZA-XAT", 13, gap=10)
    w.para(
        "Мы, ООО «Пример Техно», изучили тендерную документацию № DEMO-2026-017 и выражаем "
        "готовность участвовать в тендере по Лоту № 1 — поставка 20 (двадцати) ноутбуков."
    )
    w.para(
        "Предлагаем поставить товар в полном соответствии с техническим заданием (раздел 2) "
        "на общую сумму 183 600 000 (сто восемьдесят три миллиона шестьсот тысяч) сум, включая НДС."
    )
    w.para(
        "Срок поставки — 25 (двадцать пять) календарных дней. Гарантийный срок — 36 (тридцать шесть) "
        "месяцев. Срок действия предложения — 120 (сто двадцать) календарных дней с даты вскрытия."
    )
    w.para(
        "Biz tanlov shartlarini toʻliq qabul qilamiz va taqdim etilgan maʻlumotlarning "
        "ishonchliligini tasdiqlaymiz / Мы принимаем условия тендера и подтверждаем достоверность сведений."
    )
    w.para("К письму приложены документы согласно разделу 7 тендерной документации.")
    w.para("Контактное лицо: Тестова Анна, менеджер по тендерам, тел. +998 (00) 000-00-11, tender@primer-tekhno.example.")
    w.signature("Директор ООО «Пример Техно», Примеров П. П.")


def _bidder_a_guarantee(w: Writer) -> None:
    w.title("ГАРАНТИЙНОЕ ПИСЬМО УЧАСТНИКА (ФОРМА 1)", 12.5, gap=10)
    w.center("Тендер № DEMO-2026-017, Лот № 1", 10, gap=12)
    w.para(
        "Мы, ООО «Пример Техно» (СТИР 123456789), подтверждаем, что в случае признания нас "
        "победителем тендера № DEMO-2026-017 (Лот № 1) мы подпишем договор в установленный срок "
        "и предоставим обеспечение исполнения договора."
    )
    w.para("Гарантийный взнос (обеспечение заявки): 1 860 000 (один миллион восемьсот шестьдесят тысяч) сум, перечислен на счёт Заказчика.")
    w.para("Дата: 28.12.2026 г.")
    w.signature("Директор ООО «Пример Техно», Примеров П. П.")


def build_bidder_a_letters() -> bytes:
    """Cover letter and Form 1 guarantee letter in one document (one footer series)."""
    w = Writer()
    _bidder_a_cover(w)
    w.new_page()
    _bidder_a_guarantee(w)
    return w.tobytes()


def _offer_terms(w: "Writer", total: str, without_vat: str, vat: str, service_phone: str) -> None:
    """Terms every bid must answer (both bidders): quantity, acceptance, payment, service, price with VAT."""
    w.heading("Количество, приёмка, оплата и сервис")
    w.bullet("Количество: 20 (двадцать) штук, все ноутбуки новые, одной модели.")
    w.bullet("Доставка за наш счёт на склад Заказчика: г. Примерск, ул. Образцовая, д. 1.")
    w.bullet("При поставке передаём акт проверки комплектности и работоспособности на каждую единицу.")
    w.bullet("Согласны с порядком оплаты: 100% в течение 30 банковских дней после подписания акта приёмки.")
    w.bullet("Гарантийное обслуживание: замена или ремонт в течение не более 5 рабочих дней с момента обращения.")
    w.bullet(f"К каждому ноутбуку прилагаются инструкция по эксплуатации на русском языке и телефон сервисной службы {service_phone}.")
    w.heading("Ценовое предложение (сум)")
    w.table(
        ["№", "Позиция", "Количество", "Сумма, сум"],
        [
            ["1", "Ноутбуки, без НДС", "20 шт.", without_vat],
            ["2", "НДС 12%", "—", vat],
            ["3", "Итого с НДС", "20 шт.", total],
        ],
        (0.45, 3.2, 1.5, 2.7),
    )


def build_bidder_a_technical() -> bytes:
    w = Writer()
    w.title("ТЕХНИЧЕСКОЕ ПРЕДЛОЖЕНИЕ И ТАБЛИЦА СООТВЕТСТВИЯ", 12.5, gap=8)
    w.center("Участник: ООО «Пример Техно» (СТИР 123456789). Модель: «ПРИМЕР-15 Pro» (вымышленная)", 9.5, gap=10)
    w.heading("Таблица соответствия техническим требованиям раздела 2")
    w.table(
        ["№", "Параметр", "Требование ТЗ", "Предложение участника"],
        TECH_PROPOSAL_A,
        (0.45, 1.7, 2.7, 3.5),
    )
    w.heading("Условия поставки и гарантия")
    w.bullet("Поставка: в течение 25 календарных дней с даты заключения договора, склад Заказчика.")
    w.bullet("Гарантийный срок: 36 месяцев с даты поставки, гарантийное обслуживание по месту эксплуатации.")
    w.bullet("Все ноутбуки новые, заводская упаковка, серийные номера приведены в спецификации.")
    w.bullet("Участник гарантирует соответствие каждого параметра документам производителя.")
    _offer_terms(w, "183 600 000", "163 928 571,43", "19 671 428,57", "+998 (00) 000-00-01")
    w.signature("Директор ООО «Пример Техно», Примеров П. П.")
    return w.tobytes()


def build_bidder_a_documents() -> bytes:
    w = Writer()
    w.title("СВИДЕТЕЛЬСТВО О ГОСУДАРСТВЕННОЙ РЕГИСТРАЦИИ", 12.5, gap=2)
    w.center("Davlat roʻyxatidan oʻtganlik guvohnomasi", 9.5, gap=12)
    w.kv_rows(
        [
            ("Серия и номер", "AA № 0012345"),
            ("Дата регистрации", "12.03.2019"),
            ("Наименование", "ООО «Пример Техно»"),
            ("СТИР (ИНН)", "123456789"),
            ("Юридический адрес", "г. Примерск, ул. Тестовая, д. 5"),
            ("Регистрирующий орган", "Инспекция Примерского района (вымышленная)"),
        ]
    )
    w.new_page()
    w.title("СПРАВКА НАЛОГОВОГО ОРГАНА № 4471", 12.5, gap=2)
    w.center("Soliq ma'lumotnomasi / об отсутствии задолженности", 9.5, gap=12)
    w.kv_rows(
        [
            ("Дата выдачи", "22.12.2026"),
            ("Налогоплательщик", "ООО «Пример Техно», СТИР 123456789"),
            ("Задолженность", "по налогам и сборам отсутствует на дату выдачи"),
            ("Назначение", "участие в тендере № DEMO-2026-017"),
        ]
    )
    w.para(
        "Ushbu ma'lumotnoma soliq qarzdorligi yoʻqligini tasdiqlaydi / Настоящая справка подтверждает, "
        "что на дату выдачи у налогоплательщика отсутствует задолженность.",
        gap=6,
    )
    w.new_page()
    w.title("ДИЛЕРСКОЕ ПИСЬМО № 77", 12.5, gap=2)
    w.center("Dilerlik xati / от 15.10.2026", 9.5, gap=12)
    w.para(
        "ООО «Дистрибьютор Пример» (вымышленный производитель и дистрибьютор) уполномочивает "
        "ООО «Пример Техно» (СТИР 123456789) предлагать и поставлять ноутбуки модели «ПРИМЕР-15 Pro» "
        "для тендера № DEMO-2026-017 на территории Примерской области."
    )
    w.para("Срок действия письма: до 31.12.2026 включительно.")
    w.signature("Генеральный директор ООО «Дистрибьютор Пример»")
    return w.tobytes()


CONTRACTS_A = [
    ("14/2024", "12.05.2024", "ООО «Альфа-Образец»", "12 (двенадцать) ноутбуков", "99 000 000 сум", "14", "20.06.2024"),
    ("03/2025", "17.02.2025", "ООО «Бета-Демо»", "25 (двадцать пять) ноутбуков", "225 000 000 сум", "03", "15.04.2025"),
    ("21/2025", "09.09.2025", "ООО «Гамма-Пример»", "10 (десять) ноутбуков", "91 000 000 сум", "21", "10.11.2025"),
]


def build_bidder_a_contracts() -> bytes:
    w = Writer()
    for index, (number, date, customer, subject, total, act_no, act_date) in enumerate(CONTRACTS_A):
        if index:
            w.new_page()
        w.title(f"ДОГОВОР ПОСТАВКИ № {number} от {date}", 12.5, gap=2)
        w.center("Копия (сокращённая) для тендера № DEMO-2026-017", 9.5, gap=12)
        w.kv_rows(
            [
                ("Поставщик", "ООО «Пример Техно», СТИР 123456789"),
                ("Покупатель", f"{customer} (вымышленная организация)"),
                ("Предмет договора", f"Поставка {subject}"),
                ("Сумма договора", f"{total}, с НДС"),
                ("Период исполнения", "2024–2025 гг."),
            ]
        )
        w.heading("Акт приёмки")
        w.para(
            f"Акт приёмки-передачи товара № {act_no} от {act_date} подписан обеими сторонами. "
            "Претензий по количеству, комплектности и качеству товара не заявлено."
        )
        w.para("Копия верна.")
        w.signature("Директор ООО «Пример Техно», Примеров П. П.")
    return w.tobytes()


# ---------------------------------------------------------------------------
# Bidder B: MChJ «Namuna Savdo», STIR 987654321 (planted problems)
# ---------------------------------------------------------------------------

TECH_PROPOSAL_B = [
    ["1", "Процессор", "не менее 8 ядер, не менее 3,0 ГГц, или эквивалент", "8 ядер, 3,0 ГГц (соответствует)"],
    ["2", "Оперативная память", "не менее 16 ГБ DDR5", "8 ГБ DDR4, 3200 МГц (соответствует)"],
    ["3", "Накопитель", "SSD NVMe не менее 512 ГБ", "512 ГБ SSD NVMe (соответствует)"],
    ["4", "Диагональ экрана", "15,6 дюйма, не менее 1920×1080", "15,6 дюйма, 1920×1080 IPS (соответствует)"],
    ["5", "Видеоподсистема", "интегрированная, или эквивалент", "интегрированная (соответствует)"],
    ["6", "Аккумулятор", "не менее 45 Втч; не менее 8 часов", "48 Втч; до 8,5 часов (соответствует)"],
    ["7", "Масса", "не более 2,0 кг", "1,9 кг (соответствует)"],
    ["8", "Операционная система", "Windows 11 Pro или эквивалент", "Windows 11 Pro (соответствует)"],
    ["9", "Гарантийный срок", "не менее 24 месяцев", "24 (двадцать четыре) месяца (соответствует)"],
    ["10", "Комплектация", "сумка-чехол, мышь, блок питания", "все позиции в наличии (соответствует)"],
]


def build_bidder_b_cover() -> bytes:
    w = Writer()
    w.para("MChJ «Namuna Savdo» (ООО «Намуна Савдо»)\nСТИР 987654321 · г. Примерск, Namuna koʻchasi, 7\nИсх. № 52 от 27.12.2026", size=9.5, gap=10)
    w.para("Заказчику: ГУП «Пример Заказчик», тендерная комиссия", gap=12)
    w.title("ПИСЬМО-ЗАЯВКА", 13, gap=10)
    w.para(
        "Просим допустить MChJ «Namuna Savdo» к участию в тендере № DEMO-2026-017 (Лот № 1) — "
        "поставка 20 (двадцати) ноутбуков."
    )
    w.para(
        "Предлагаем поставить товар на сумму 176 000 000 (сто семьдесят шесть миллионов) сум, включая НДС. "
        "Срок поставки — 30 (тридцать) календарных дней. Срок действия предложения — 90 (девяносто) дней."
    )
    w.para(
        "Компания имеет опыт поставки аналогичного оборудования: за 2024–2025 гг. заключено более "
        "5 (пяти) договоров. Копии договоров прилагаются."
    )
    w.para("Наши ноутбуки полностью соответствуют техническому заданию.")
    w.para("Biz tanlov shartlariga roziligimizni bildiramiz / Мы согласны с условиями тендера.")
    w.para("Контактное лицо: Namunova Dilnoza, тел. +998 (00) 000-00-22, info@namuna-savdo.example.")
    w.signature("Директор MChJ «Namuna Savdo», Namunov N. N.")
    w.new_page()
    w.title("ГАРАНТИЙНОЕ ПИСЬМО УЧАСТНИКА (ФОРМА 1)", 12.5, gap=10)
    w.center("Тендер № DEMO-2026-017, Лот № 1", 10, gap=12)
    w.para(
        "Мы, MChJ «Namuna Savdo» (СТИР 987654321), подтверждаем, что в случае признания нас "
        "победителем тендера № DEMO-2026-017 (Лот № 1) мы подпишем договор в установленный срок "
        "и предоставим обеспечение исполнения договора."
    )
    w.para("Гарантийный взнос (обеспечение заявки): 1 760 000 (один миллион семьсот шестьдесят тысяч) сум.")
    w.para("Дата: 27.12.2026 г.")
    w.signature("Директор MChJ «Namuna Savdo», Namunov N. N.")
    return w.tobytes()


def build_bidder_b_technical() -> bytes:
    w = Writer()
    w.title("ТЕХНИЧЕСКОЕ ПРЕДЛОЖЕНИЕ И ТАБЛИЦА СООТВЕТСТВИЯ", 12.5, gap=8)
    w.center("Участник: MChJ «Namuna Savdo» (СТИР 987654321). Модель: «ПРИМЕР-Лайт 15» (вымышленная)", 9.5, gap=10)
    w.heading("Таблица соответствия техническим требованиям раздела 2")
    w.table(
        ["№", "Параметр", "Требование ТЗ", "Предложение участника"],
        TECH_PROPOSAL_B,
        (0.45, 1.7, 2.7, 3.5),
    )
    w.heading("Условия поставки и гарантия")
    w.bullet("Поставка: в течение 30 календарных дней с даты заключения договора.")
    w.bullet("Гарантийный срок: 24 месяца согласно гарантийному талону производителя.")
    w.bullet("Участник гарантирует соответствие каждого параметра документам производителя.")
    _offer_terms(w, "176 000 000", "157 142 857,14", "18 857 142,86", "+998 (00) 000-00-02")
    w.signature("Директор MChJ «Namuna Savdo», Namunov N. N.")
    return w.tobytes()


def build_bidder_b_documents(scan_jpeg: bytes) -> bytes:
    w = Writer()
    w.title("СВИДЕТЕЛЬСТВО О ГОСУДАРСТВЕННОЙ РЕГИСТРАЦИИ", 12.5, gap=2)
    w.center("Davlat roʻyxatidan oʻtganlik guvohnomasi", 9.5, gap=12)
    w.kv_rows(
        [
            ("Серия и номер", "AA № 0067890"),
            ("Дата регистрации", "22.06.2021"),
            ("Наименование", "MChJ «Namuna Savdo»"),
            ("СТИР (ИНН)", "987654321"),
            ("Юридический адрес", "г. Примерск, Namuna koʻchasi, 7"),
        ]
    )
    w.new_page()
    w.title("ЛИЦЕНЗИЯ № L-0457", 12.5, gap=2)
    w.center("Litsenziya / оптовая торговля компьютерной техникой", 9.5, gap=12)
    w.kv_rows(
        [
            ("Лицензиат", "MChJ «Namuna Savdo», СТИР 987654321"),
            ("Вид деятельности", "оптовая торговля компьютерной техникой"),
            ("Дата выдачи", "01.01.2023"),
            ("Срок действия", "до 31.12.2025"),
        ]
    )
    w.new_page()
    w.title("СПРАВКА НАЛОГОВОГО ОРГАНА № 9120", 12.5, gap=2)
    w.center("Soliq ma'lumotnomasi / от 18.12.2026", 9.5, gap=12)
    w.kv_rows(
        [
            ("Дата выдачи", "18.12.2026"),
            ("Налогоплательщик", "MChJ «Namuna Savdo», СТИР 987654321"),
            ("Задолженность", "по налогам и сборам отсутствует на дату выдачи"),
        ]
    )
    # Page 4 is a scanned image of a reference letter for another company.
    w.image_page(scan_jpeg)
    return w.tobytes()


def build_reference_letter_scan() -> bytes:
    """Render a text page to a grey JPEG so it has no text layer once embedded."""
    w = Writer()
    w.title("СПРАВКА-РЕКОМЕНДАЦИЯ № 33", 13, gap=4)
    w.center("от 10.12.2026 г., г. Примерск", 10, gap=18)
    w.para(
        "Настоящей справкой ООО «Пример Снаб» (СТИР 444333222) подтверждает, что ООО «Пример Логистик» "
        "(СТИР 555111222) в 2024–2025 гг. выполняло поставки компьютерной техники по договорам с "
        "ООО «Пример Снаб». Поставки выполнены в срок, претензий нет."
    )
    w.para(
        "Ushbu ma'lumotnoma ООО «Пример Логистик» uchun berilgan / Справка выдана по запросу "
        "ООО «Пример Логистик» для участия в закупках."
    )
    w.para("Рекомендация действительна до 31.12.2026.", gap=24)
    w.signature("Директор ООО «Пример Снаб», Пример С. С.")
    assert w.doc.page_count == 1, "reference letter must fit on one page"
    w.tobytes()  # adds the demo footer in place, so the scan carries it too
    pixmap = w.doc[0].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY, alpha=False)
    return pixmap.tobytes("jpg", jpg_quality=72)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_all() -> dict[str, dict[str, bytes]]:
    scan = build_reference_letter_scan()
    return {
        "tender": {"Тендерная документация.pdf": build_tender()},
        "bidder-a": {
            "01 Письмо-заявка и гарантия.pdf": build_bidder_a_letters(),
            "02 Техническое предложение.pdf": build_bidder_a_technical(),
            "03 Регистрационные и налоговые документы.pdf": build_bidder_a_documents(),
            "04 Договоры и акты.pdf": build_bidder_a_contracts(),
        },
        "bidder-b": {
            "01 Письмо-заявка и гарантия.pdf": build_bidder_b_cover(),
            "02 Техническое предложение.pdf": build_bidder_b_technical(),
            "03 Документы и справки.pdf": build_bidder_b_documents(scan),
        },
    }


def write_samples(out_dir: Path) -> list[Path]:
    written: list[Path] = []
    for folder, files in build_all().items():
        target = out_dir / folder
        target.mkdir(parents=True, exist_ok=True)
        for old in target.glob("*.pdf"):
            old.unlink()
        for name, data in files.items():
            path = target / name
            path.write_bytes(data)
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    args = parser.parse_args(argv)
    for path in write_samples(args.out):
        print(f"{path} ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
