#!/usr/bin/env python3
"""Test LLM directly with actual content."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.llm_extractor import LLMExtractor
import json
import logging

# Set up detailed logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s - %(name)s - %(message)s')

async def test_llm():
    settings = Settings()
    extractor = LLMExtractor(settings)

    # Actual content from the crawl
    content = """=== MAIN PAGE ===
top of page
HOME
![JPO Logo.png](https://static.wixstatic.com/media/724b33_46e9dbb8837445cb83c8211302340d88~mv2.png/v1/fill/w_241,h_202,al_c,q_85,usm_0.66_1.00_0.01,enc_avif,quality_auto/JPO%20Logo.png)
### "The best vision is Insight" - Malcolm S Forbes
##  OUR FOCUS
Your business is there to serve you, to help you to achieve the lifestyle and aspiration you want. But often, most business owners become a servant to their business – it becomes their 'master'. As with all things in life "If you fail to Plan you Plan to Fail". At JPO, our mission is to help our clients to 'design' a business that serves them.
Facebook
Twitter
Pinterest
Tumblr
Copy Link
Link Copied
###### [READ MORE...](https://www.jpo.com.au/project-1)
## What we can do to help you with
At JPO, we believe everyone deserves a chance to perform better in business. We work with all businesses that are faced with challenges and also opportunities. We help to problem-solve and also equip our clients to manage their business and their personal well being.
  * Be accountable to your goals
  * Identify and make changes to improve
  * Create systems to run your business
  * Set milestones and monitor your progress
  * Provide some leadership and direction
  * Work on opportunities and gaps in the business
## What our clients say about us
"Great accounting and bookkeeping service! Always have great advice and help you through any problems you have. Highly recommend these guys!!!." [Neva Trewern](https://www.facebook.com/N.Trewern?__tn__=%2CdlC-R-R&eid=ARCuRzB4gzatHDvrveqYuZu4As32Cixf7sE_iVRXwaZ_dPep9pEE8HhOa8a_l4Q9AbicD9Q8H03JzGLc&hc_ref=ARSrv6yed5HJsmgDpzt18Al8SWAyHvVhYiq3gt-6H6VTh6tC9R2DlxZ35Nue_7ase2c)
## Our Values
The Power of Understanding.
To be effective in what we do is to first seek to understand our client's challenges and their desired outcomes. This way, we can be focus on the what and why of what we do.
The Power of Commitment.
It is important for us that when we start we to finish great.
"""

    print("Testing LLM extraction with JPO content...")
    print("="*80)

    result = await extractor.extract(
        company_url="https://www.jpo.com.au",
        main_content=content,
        bio_content=[]
    )

    print("\n" + "="*80)
    print("LLM Extraction Result:")
    print("="*80)
    print(f"Company Name: {result.company_name}")
    print(f"Confidence Score: {result.confidence_score}")
    print(f"Business Segment: {result.business_segment}")
    print(f"Out of Scope: {result.out_of_scope}")
    print(f"Office Phone: {result.office_phone}")
    print(f"Office Email: {result.office_email}")
    print(f"Location: {result.associated_location}")
    print(f"Decision Makers: {len(result.decision_makers)}")
    for i, dm in enumerate(result.decision_makers, 1):
        print(f"  {i}. {dm.name} - {dm.title}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_llm())
