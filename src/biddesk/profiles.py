"""The three gallery firms. FICTIONAL firms with real-shaped profiles; the solicitations are real.

Coordinates are city centroids (OKC 35.4676,-97.5164; Norman 35.2226,-97.4395; Tulsa 36.1540,-95.9928),
used only for the owner's distance rule. Set-aside codes follow SAM.gov typeOfSetAside values
(SBA, WOSB, EDWOSB, SDVOSBC, 8A, HZC) from the Get Opportunities API docs.
"""
from __future__ import annotations
from .models import FirmProfile, OwnerRule, PastPerformance

REAL_SOLICITATION_TYPES = ["Solicitation", "Combined Synopsis/Solicitation", "Presolicitation", "Sources Sought"]

RED_CEDAR = FirmProfile(
    slug="red-cedar",
    name="Red Cedar Facility Services",
    city="Oklahoma City", state="OK", lat=35.4676, lon=-97.5164,
    what="Janitorial and grounds maintenance for federal buildings",
    naics=["561720", "561730"],
    set_asides=["WOSB", "EDWOSB", "SBA"],
    capabilities=(
        "Woman-owned small business (WOSB) providing janitorial, custodial, floor care, window cleaning, "
        "and grounds maintenance (mowing, trimming, irrigation, snow and ice removal) for federal and state "
        "facilities in Oklahoma and neighboring states. 38 W-2 employees, CIMS-GB certified, e-Verify, "
        "OSHA 30 supervisors, green cleaning program, QC inspections logged in a CMMS."),
    past_performance=[
        PastPerformance(customer="GSA Region 7", title="Custodial services, Oklahoma City Federal Building",
                        naics="561720", value_usd=1_240_000, period="2022-2025",
                        keywords=["custodial", "janitorial", "federal building", "floor care", "restrooms", "trash", "recycling", "day porter"]),
        PastPerformance(customer="Tinker AFB (72 ABW)", title="Grounds maintenance, cantonment area",
                        naics="561730", value_usd=880_000, period="2021-2024",
                        keywords=["grounds", "mowing", "trimming", "landscaping", "irrigation", "snow removal", "herbicide"]),
        PastPerformance(customer="Oklahoma Office of Management and Enterprise Services", title="Janitorial, state office complex",
                        naics="561720", value_usd=610_000, period="2020-2023",
                        keywords=["janitorial", "office", "carpet", "windows", "supplies", "green cleaning"]),
        PastPerformance(customer="VA Oklahoma City Health Care System", title="Housekeeping support, outpatient clinic",
                        naics="561720", value_usd=420_000, period="2023-2025",
                        keywords=["housekeeping", "clinic", "healthcare", "infection control", "terminal cleaning", "medical"]),
    ],
    rules=[
        OwnerRule(id="rc-types", text="Only real solicitations: no award notices, no justifications, no special notices.",
                  kind="notice_types", value=REAL_SOLICITATION_TYPES),
        OwnerRule(id="rc-setaside", text="Only bid where we are eligible: WOSB, EDWOSB, total small business, or unrestricted.",
                  kind="set_aside_eligible", value=["WOSB", "EDWOSB", "SBA", "", "NONE"]),
        OwnerRule(id="rc-distance", text="No bids over 200 miles from Oklahoma City.", kind="max_distance_miles", value=200),
        OwnerRule(id="rc-base", text="No bids under a 12-month base period.", kind="min_base_months", value=12),
    ],
)

SOONER_SYSTEMS = FirmProfile(
    slug="sooner-systems",
    name="Sooner Systems LLC",
    city="Norman", state="OK", lat=35.2226, lon=-97.4395,
    what="IT support, help desk, and cybersecurity staffing",
    naics=["541512", "541519"],
    set_asides=["SDVOSBC", "SBA"],
    capabilities=(
        "Service-disabled veteran-owned small business (SDVOSB) delivering Tier 1-3 help desk, desktop and "
        "network administration, Microsoft 365 and Azure administration, RMF/ATO documentation, vulnerability "
        "management (ACAS/Nessus), and cybersecurity staffing. 22 cleared staff (Secret), CMMC Level 2 "
        "self-assessed, ITIL 4 and Security+ across the bench, 24x7 NOC in Norman, OK."),
    past_performance=[
        PastPerformance(customer="US Army Corps of Engineers, Tulsa District", title="IT help desk and desktop support",
                        naics="541512", value_usd=2_100_000, period="2022-2025",
                        keywords=["help desk", "desktop", "tier 1", "tier 2", "service desk", "ITIL", "ticketing", "end user", "information technology", "IT services", "IT support"]),
        PastPerformance(customer="FAA Mike Monroney Aeronautical Center", title="Network administration and cybersecurity support",
                        naics="541519", value_usd=1_650_000, period="2021-2024",
                        keywords=["network", "cybersecurity", "cyber", "security", "SOC", "SIEM", "SOAR", "incident response", "RMF", "ATO", "vulnerability", "ACAS", "STIG", "firewall", "ISSO"]),
        PastPerformance(customer="Oklahoma National Guard", title="Systems administration and Microsoft 365 migration",
                        naics="541512", value_usd=740_000, period="2023-2025",
                        keywords=["systems administration", "Microsoft 365", "Azure", "migration", "Active Directory", "Exchange"]),
    ],
    rules=[
        OwnerRule(id="ss-types", text="Only real solicitations: no award notices, no justifications, no special notices.",
                  kind="notice_types", value=REAL_SOLICITATION_TYPES),
        OwnerRule(id="ss-setaside", text="Only bid where we are eligible: SDVOSB, total small business, or unrestricted.",
                  kind="set_aside_eligible", value=["SDVOSBC", "SBA", "", "NONE"]),
        OwnerRule(id="ss-pp", text="Only bid where we have two past-performance matches.", kind="min_past_performance_matches", value=2),
    ],
)

PLAINS_MED = FirmProfile(
    slug="plains-med",
    name="Plains Med Staffing",
    city="Tulsa", state="OK", lat=36.1540, lon=-95.9928,
    what="Nursing and allied health staffing for VA and IHS facilities",
    naics=["561320", "621399"],
    set_asides=["8A", "SBA"],
    capabilities=(
        "SBA 8(a) certified staffing firm placing RNs, LPNs, CNAs, medical technologists, respiratory therapists, "
        "and physical therapists at VA medical centers and Indian Health Service facilities across Oklahoma, "
        "Kansas, and Texas. Joint Commission certified (Health Care Staffing Services), 140 credentialed clinicians "
        "on the bench, 48-hour fill on urgent requests, in-house credentialing and primary source verification."),
    past_performance=[
        PastPerformance(customer="VA Eastern Oklahoma Health Care System (Muskogee)", title="Registered nurse staffing, inpatient units",
                        naics="561320", value_usd=3_400_000, period="2022-2025",
                        keywords=["registered nurse", "RN", "nursing", "inpatient", "staffing", "VA", "medical center", "per diem"]),
        PastPerformance(customer="Indian Health Service, Oklahoma City Area", title="Allied health staffing, Claremore Indian Hospital",
                        naics="621399", value_usd=1_150_000, period="2021-2024",
                        keywords=["allied health", "respiratory therapist", "medical technologist", "laboratory", "IHS", "hospital"]),
        PastPerformance(customer="VA North Texas Health Care System", title="LPN and CNA staffing, community living center",
                        naics="561320", value_usd=980_000, period="2023-2025",
                        keywords=["LPN", "CNA", "nursing assistant", "long term care", "community living center", "geriatric"]),
    ],
    rules=[
        OwnerRule(id="pm-types", text="Only real solicitations: no award notices, no justifications, no special notices.",
                  kind="notice_types", value=REAL_SOLICITATION_TYPES),
        OwnerRule(id="pm-setaside", text="Only bid where we are eligible: 8(a), total small business, or unrestricted.",
                  kind="set_aside_eligible", value=["8A", "SBA", "", "NONE"]),
        OwnerRule(id="pm-days", text="Never bid on a solicitation with under 10 days to close.", kind="min_days_to_close", value=10),
    ],
)

PROFILES: dict[str, FirmProfile] = {p.slug: p for p in (RED_CEDAR, SOONER_SYSTEMS, PLAINS_MED)}


def get(slug: str) -> FirmProfile:
    return PROFILES[slug]
