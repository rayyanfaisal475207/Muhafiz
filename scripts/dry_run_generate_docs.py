"""Dry-run: generate ground-truth content (structured fields via template +
LLM narrative with entity profiles injected) for the 3-document dry-run slice
per SYNTHETIC_DATASET_PLAN.md Step 1.

Writes data/memory/_ground_truth/<doc_id>.json for each document.
"""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from groq import AsyncGroq

ROOT = Path(__file__).resolve().parent.parent
GT_DIR = ROOT / "data" / "memory" / "_ground_truth"
GT_DIR.mkdir(parents=True, exist_ok=True)

MODEL = "llama-3.3-70b-versatile"


def _client() -> AsyncGroq:
    key = os.environ.get("GROQ_API_KEY_1") or os.environ.get("GROQ_API_KEY")
    return AsyncGroq(api_key=key)


async def _generate(system: str, user: str, max_tokens: int = 700) -> str:
    client = _client()
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


SYSTEM_PROMPT = (
    "آپ اردو میں پاکستانی پولیس کے سرکاری اور نیم رسمی دستاویزات لکھنے میں مہارت رکھتے ہیں۔ "
    "صرف خالص، فطری اردو نثر لکھیں — انگریزی حروف تہجی استعمال نہ کریں سوائے ان مخصوص الفاظ کے جو دیے گئے ہیں۔ "
    "صرف مطلوبہ متن واپس کریں، کوئی اضافی وضاحت یا سرخی شامل نہ کریں۔"
)


async def generate_fir_tehrir() -> str:
    user = (
        "درج ذیل تفصیلات کی بنیاد پر ایک FIR کی تحریر (مدعی کا اپنا بیان، پہلا شخص) لکھیں، "
        "تقریباً 90-130 الفاظ میں:\n\n"
        "مدعی کا نام: احمد رضا قریشی (تحریر میں نام دہرانے کی ضرورت نہیں، پہلا شخص 'میں' استعمال کریں)\n"
        "ملزم کا نام: عمران ستار ولد غلام ستار\n"
        "واقعہ: مدعی نے تھانہ رمنہ کی حدود میں ملزم عمران ستار کو ایک غیر لائسنس یافتہ پستول (بور 30، فرانسیسی ساخت، میگزین میں 6 گولیاں) کے ساتھ مشکوک انداز میں دیکھا اور فوری طور پر گشت پر مامور پولیس کو اطلاع دی۔ پولیس نے موقع پر پہنچ کر ملزم کو حراست میں لے کر پستول برآمد کیا۔\n"
        "لہجہ: رسمی، درخواست نما، تھوڑا سا تفصیلی، جیسے حقیقی FIR تحریر میں ہوتا ہے۔"
    )
    return await _generate(SYSTEM_PROMPT, user)


async def generate_witness1_narrative() -> str:
    user = (
        "درج ذیل تفصیلات کی بنیاد پر ایک گواہ کا 161 ضابطہ فوجداری کا بیان (پہلا شخص) لکھیں، تقریباً 90-130 الفاظ میں:\n\n"
        "گواہ خود کو نان‌رسمی طور پر 'احمد رضا' کہتا ہے (مکمل نام نہیں دہراتا کیونکہ یہ خود اسی کا بیان ہے)۔\n"
        "گواہ نے ملزم کو مشکوک حالت میں دیکھا — پہلی بار ملزم کا نام 'عمران ستار' لیں، اس کے بعد پورے بیان میں ملزم کے لیے صرف 'ملزم' یا 'وہ شخص' استعمال کریں، دوبارہ نام نہ دہرائیں (تاکہ کسی اور اسی نام کے شخص سے الجھن نہ ہو)۔\n"
        "یہ منظر 'عمران ستار کی دکان' کے سامنے پیش آیا — دکان کا حوالہ صرف مقام بتانے کے لیے ایک عام فقرے کے طور پر دیں (جیسے کسی بھی جگہ کا نام لیا جاتا ہے)، کوئی وضاحت یا نوٹ شامل نہ کریں کہ یہ کوئی اور شخص ہے — بس فطری انداز میں مقام کا ذکر کریں اور آگے بڑھ جائیں۔\n"
        "پستول ملزم کے ہاتھ میں دیکھنے اور پولیس کو بلانے کی تفصیل بیان کریں، صرف 'ملزم' یا 'اس' کا حوالہ دیں (واضح رہے کہ 'اس' سے مراد ہمیشہ ملزم ہی ہے)۔\n"
        "لہجہ: سادہ، بول چال کے قریب، لیکن باضابطہ بیان کے انداز میں۔ صرف معیاری، فطری اردو الفاظ استعمال کریں، من گھڑت الفاظ نہ بنائیں۔"
    )
    return await _generate(SYSTEM_PROMPT, user)


async def generate_witness2_narrative() -> str:
    user = (
        "درج ذیل تفصیلات کی بنیاد پر ایک دوسرے گواہ (مشیر/برآمدگی کا گواہ) کا بیان لکھیں، تقریباً 70-100 الفاظ میں:\n\n"
        "گواہ کا نام: ظفر اقبال ولد اقبال حسین\n"
        "یہ گواہ پستول کی برآمدگی کے وقت موجود تھا اور پولیس نے اسے بطور مشیر (برآمدگی کا گواہ) شامل کیا۔\n"
        "بیان میں یہ بتائیں کہ ملزم عمران ستار سے پستول (بور 30) کیسے برآمد ہوئی، اور گواہ نے برآمدگی میمو پر دستخط/انگوٹھا لگایا۔\n"
        "لہجہ: مختصر، رسمی، برآمدگی گواہ کے بیان جیسا خشک اور طریقہ کار پر مبنی۔"
    )
    return await _generate(SYSTEM_PROMPT, user)


async def main():
    fir_tehrir, w1_narrative, w2_narrative = await asyncio.gather(
        generate_fir_tehrir(), generate_witness1_narrative(), generate_witness2_narrative()
    )

    fir_doc = {
        "doc_id": "FIR-2026-ARMS-001",
        "doc_type": "FIR",
        "source": "synthetic",
        "category": "Illegal Weapon Possession",
        "police_station": "Ramna",
        "date_registered": "2026-05-02 14:20",
        "sections": "13 Arms Ordinance 1965",
        "language": "ur",
        "rendering": "clean",
        "case_id": "CASE-DRY-001",
        "entities": ["P-DRY-001", "P-DRY-003", "WPN-DRY-001"],
        "structured_fields": {
            "fir_number": "FIR-2026-ARMS-001",
            "police_station": "تھانہ رمنہ",
            "date_time": "2026-05-02 14:20",
            "complainant_name": "احمد رضا قریشی",  # variant 1 (canonical)
            "complainant_father_name": "محمد قریشی",
            "complainant_cnic": "00000-9119877-0",  # matches build_entity_roster.py's cnic('p003-fix')
            "complainant_address": "مکان 7، سیکٹر جی-8/2، اسلام آباد",
            "accused_name": "عمران ستار",
            "accused_father_name": "غلام ستار",
            "accused_address": "مکان نمبر 12، گلی 4، جی-9/1، اسلام آباد",
            "sections": "دفعہ 13 آرمز آرڈیننس 1965",
        },
        "narrative_tehrir": fir_tehrir,
    }

    w1_doc = {
        "doc_id": "WITNESS-FIR-2026-ARMS-001-01",
        "doc_type": "Witness Statement",
        "source": "synthetic",
        "related_fir": "FIR-2026-ARMS-001",
        "police_station": "Ramna",
        "date_registered": "2026-05-03",
        "language": "ur",
        "rendering": "handwritten",
        "case_id": "CASE-DRY-001",
        "entities": ["P-DRY-002", "P-DRY-003", "P-DRY-001", "WPN-DRY-001"],
        "structured_fields": {
            "fir_reference": "FIR-2026-ARMS-001",
            "witness_name": "احمد رضا قرشی",  # variant 3 (clerical misspelling, recording officer's transcription)
            "witness_father_name": "محمد قریشی",
            "witness_cnic": "00000-9119877-0",  # matches build_entity_roster.py's cnic('p003-fix')
            "witness_address": "مکان 7، سیکٹر جی-8/2، اسلام آباد",
            "witness_phone": "0300-1234567",
            "date_time_recorded": "2026-05-03 11:00",
            "recording_officer": "محرر تھانہ رمنہ",
        },
        "narrative_statement": w1_narrative,
    }

    w2_doc = {
        "doc_id": "WITNESS-FIR-2026-ARMS-001-02",
        "doc_type": "Witness Statement",
        "source": "synthetic",
        "related_fir": "FIR-2026-ARMS-001",
        "police_station": "Ramna",
        "date_registered": "2026-05-02",
        "language": "ur",
        "rendering": "handwritten",
        "case_id": "CASE-DRY-001",
        "entities": ["P-DRY-004", "P-DRY-001", "WPN-DRY-001"],
        "structured_fields": {
            "fir_reference": "FIR-2026-ARMS-001",
            "witness_name": "ظفر اقبال",
            "witness_father_name": "اقبال حسین",
            "witness_cnic": "00000-5731097-8",  # matches build_entity_roster.py's cnic('p004-fix')
            "witness_address": "مکان 22، بارہ کہو، اسلام آباد",
            "date_time_recorded": "2026-05-02 15:10",
            "recording_officer": "محرر تھانہ رمنہ",
        },
        "narrative_statement": w2_narrative,
    }

    for doc in (fir_doc, w1_doc, w2_doc):
        out_path = GT_DIR / f"{doc['doc_id']}.json"
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", out_path)


if __name__ == "__main__":
    asyncio.run(main())
