import os
import json
import time
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import List

load_dotenv()

class BriefingItem(BaseModel):
    headline: str = Field(description="핵심 요약 제목 (기사 제목을 그대로 쓰지 말고 간결히 재구성)")
    summary: str = Field(description="2~3문장 핵심 요약")
    impact: str = Field(description="시사점/파급효과 1줄")
    source_url: str = Field(description="원문 기사 URL, 경제 지표 요약 항목은 빈 문자열")

class BriefingSection(BaseModel):
    category: str = Field(description="카테고리명 (거시 경제 & 주요 지표, 주요 기업 동향, AX · RX · 디지털 트윈 & 로보틱스, 국제 정세, 국내 정치, 스포츠 중 하나)")
    items: List[BriefingItem]

class DailyBriefing(BaseModel):
    title: str = Field(description="브리핑 전체 제목")
    sections: List[BriefingSection]
    closing_comment: str = Field(description="마무리 코멘트 1~2문장")
    short_summary_for_sns: str = Field(description="500자 내외 SNS 요약")

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# Mock input to keep prompt simple but test JSON output
mock_articles_text = ""
for idx in range(25):
    mock_articles_text += f"[{idx+1}] 제목: 기사 제목 {idx+1}입니다.\n링크: http://example.com/{idx+1}\n설명: 기사 {idx+1}에 대한 자세한 설명입니다. IT 트렌드, 정치, 기업, 스포츠 등 다양한 분야에 대해 서술합니다.\n\n"

prompt = f"""당신은 일일 뉴스 브리핑 편집장입니다.
아래의 뉴스 기사 목록을 분석하여, 카테고리별로 분류하고 핵심 내용을 요약한 브리핑 원고를 JSON 형식으로 작성해 주세요.

[뉴스 기사 목록]
{mock_articles_text}

[카테고리 분류 규칙]
반드시 다음 6개 카테고리를 모두 포함하여 작성하세요 (기사가 부족하거나 없는 카테고리도 절대 생략하지 말고 반드시 포함해야 합니다):
1. "거시 경제 & 주요 지표"
2. "주요 기업 동향"
3. "AX · RX · 디지털 트윈 & 로보틱스"
4. "국제 정세"
5. "국내 정치"
6. "스포츠"

[작성 지침]
1. 모든 카테고리(6개 분야)가 결과에 반드시 포함되어야 하며, 각 카테고리마다 아이템이 최소 3개 이상 작성되어야 합니다.
   - 만약 특정 분야(예: 스포츠, 국내 정치 등)에 해당하는 뉴스 기사가 없거나 부족하면, 제공된 기사 중 연관성이 있는 것(스포츠 스폰서십, 정부의 기술 규제 등)을 다각도로 해석해 채워 넣으세요.
2. 각 기사 항목에 반드시 원문 기사 링크(source_url)를 포함하세요.
3. 모든 섹션의 각 항목에 시사점/파급효과(impact)를 1줄로 포함하세요.
4. headline은 핵심을 간결하게 재구성하세요.
5. summary는 반드시 단 1문장으로만 요약하세요. (8192 출력 토큰 한계를 넘지 않기 위해 요약은 반드시 1문장이어야 합니다.)
6. closing_comment는 마무리 코멘트 단 1문장으로 작성하세요.
7. short_summary_for_sns는 전체 브리핑을 200자 내외로 요약한 텍스트입니다.
"""

print("Calling gemini-3.5-flash...")
try:
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=DailyBriefing
        )
    )
    print("Success! Response Text length:", len(response.text) if response.text else 0)
    print("Finish Reason:", response.candidates[0].finish_reason if response.candidates else "unknown")
    print("Raw Response Text:")
    print(response.text)
    print("\nParsed object exists:", response.parsed is not None)
except Exception as e:
    print("Error calling Gemini API:", e)
