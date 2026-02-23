Using JustCall as our calling system, we want to define how we structure & create campaigns

 

Hierarchy
A hierarchy of : Country > Segment > State > Group (Professional Body, skippable hierarchy step) > Labels (voicemail, callback, email etc) > Mobile/Office number (Likely Decision Maker or Gate Keeper).
where we require a minimum of 100 leads on creation, if there’s less or we are missing data, we step up a level in the hierarchy.

This provides us good structure into having enough leads for a calling session while enabling us to be more detailed in our understanding and connection to our customers for better conversations & connecting value propositions.

The hierarchy is then set in the campaign name as short-key, providing campaigns like the below, with a month/year added _02_26




OBC_AU_ACC_NSW_CAANZ_FR_MOB_02_26	    AU Accounting, NSW, CAANZ, Fresh, Mobile
OBC_AU_ACC_VIC_CAANZ_FR_OFF_09_25       AU Accounting, VIC, CAANZ, Fresh, Office
OBC_AU_ACC_NSW_CAANZ_VM_07_25           AU Accounting, NSW, CAANZ, Left Voicemail
OBC_AU_ACC_VIC_VM_12_25	                AU Accounting, VIC, Left Voicemail
OBC_AU_ACC_TAS_CPA_11_25                AU Accounting, TAS, CPA
OBC_NZ_LAW_AKL_LS_FR_03_26	            NZ Law, Auckland, Law Society, Fresh
OBC_NZ_BAR_02_26                        NZ Barristers
OBC_NZ_LAW_AKL_RE_EMAIL_02_26           NZ Law, Auckland, Re-engaement - callbacks after email etc
OBC_ is prefixed for as ‘Outbound Call’ for identifying revenue channel for strategic purposes.

Lead Requirements
To be part of a campaign, leads must meet the following:

label != customer

not_relevant != true

primary_location exists

Linked campaigns are not created within last 3 months

 

Programatic structure for thinking through creation: 



for each (country, vertical) pair:
    query companies where:
      - segment matches vertical
      - primary_location.country_code matches country
      - customer != true
      - not_relevant != true
      - has at least one phone number (company or people)
    if count >= 300:
        split by region (primary_location.region)
        for each state bucket:
            if state_count >= 200:
                further split by professional_body if body_count >= 100
                create campaign(s)
            else:
                add to parent country+vertical campaign
    else: 
        create campaign
