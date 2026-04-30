"""Publisher tier lists and weekday rotation logic."""

from __future__ import annotations

import datetime

P0_PUBLISHERS: list[str] = [
    "employers.io", "Joblift", "JobGet", "Snagajob", "Jobcase",
    "Monster", "Allthetopbananas", "JobRapido", "Talent.com", "Talroo",
    "ZipRecruiter", "OnTimeHire", "Indeed", "Sercanto", "YadaJobs",
    "Hokify", "Upward.net", "JobCloud", "Jooble", "Nurse.com",
    "Geographic Solutions", "Reed", "Jobbsafari.se", "Jobbland",
    "Handshake", "1840",
]

P1_P2_PUBLISHERS: list[str] = [
    "JobSwipe", "Jobbird.de", "Tideri", "Manymore.jobs", "ClickaJobs",
    "MyJobScanner", "Job Traffic", "Jobtome", "Propel", "AllJobs",
    "Jora", "EarnBetter", "WhatJobs", "J-Vers", "Adzuna",
    "Galois", "Mindmatch.ai", "Myjobhelper", "TransForce", "CV Library",
    "CDLlife", "PlacedApp", "IrishJobs", "Praca.pl", "AppJobs",
    "OfferUp", "JobsInNetwork", "Jobsora", "StellenSMS", "Dice",
    "SonicJobs", "Botson.ai", "CMP Jobs", "Health Ecareers", "Hokify",
    "JobHubCentral", "BoostPoint", "Jobs In Japan", "Daijob.com",
    "GaijinPot", "GoWork.pl", "deBanenSite.nl", "Pracuj.pl", "Xing",
    "PostJobFree", "Jobsdb", "Stellenanzeigen.de", "Jobs.at", "Jobs.ch",
    "JobUp", "Jobwinner", "Topjobs.ch", "Vetted Health", "Arya by Leoforce",
    "Welcome to the Jungle", "JobMESH", "Bakeca.it", "Stack Overflow",
    "Diversity Jobs", "Laborum", "Curriculum", "American Nurses Association",
    "Profesia", "CareerCross", "Jobs.ie", "Nexxt", "Resume-Library.com",
    "Women for Hire", "Professional Diversity Network", "Rabota.bg",
    "Zaplata.bg", "Jobnet", "New Zealand Jobs", "Nationale Vacaturebank",
    "Intermediair", "eFinancialCareers", "Profession.hu", "Job Bank",
    "Personalwerk", "Yapo", "Karriere.at", "SAPO Emprego", "Catho",
    "Totaljobs", "Handshake", "Ladders.com", "Gumtree", "Instawork",
    "LinkedIn", "Facebook", "Instagram", "Google Ads", "Craigslist",
    "Reddit", "YouTube", "Spotify", "Jobbland", "Wonderkind",
    "adway.ai", "HeyTempo", "Otta", "Info Jobs", "Vagas",
    "Visage Jobs", "Hunar.ai", "CollabWORK", "Arbeitnow", "Doximity",
    "VietnamWorks", "JobKorea", "JobIndex", "HH.ru", "Consultants 500",
    "YM Careers", "Dental Post", "Foh and Boh", "Study Smarter",
    "Pnet", "Remote.co", "FATj", "Expresso Emprego", "Bravado",
]

# Sort P1/P2 alphabetically and split into 3 batches for weekly rotation.
_P1_P2_SORTED = sorted(P1_P2_PUBLISHERS)
_BATCH_SIZE = len(_P1_P2_SORTED) // 3
P1_P2_BATCHES: list[list[str]] = [
    _P1_P2_SORTED[:_BATCH_SIZE],
    _P1_P2_SORTED[_BATCH_SIZE:_BATCH_SIZE * 2],
    _P1_P2_SORTED[_BATCH_SIZE * 2:],
]


def get_todays_publishers() -> tuple[str | None, list[str] | None, str | None, str | None]:
    """Return (label, publishers, coverage_label, next_label) for today.

    Mon/Thu cover P0; Tue/Wed/Fri rotate through P1/P2 batches by ISO week.
    Weekends return all-None and the caller should skip the run.
    """
    today = datetime.date.today()
    weekday = today.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    week_num = today.isocalendar()[1]

    schedule: dict[int, tuple[str, list[str], str, str]] = {
        0: ("P0", P0_PUBLISHERS, "P0 publishers", "P1/P2 Batch 1 Tuesday"),
        1: (
            "P1/P2 Batch 1",
            P1_P2_BATCHES[week_num % 3],
            "P1/P2 Batch 1",
            "P1/P2 Batch 2 Wednesday",
        ),
        2: (
            "P1/P2 Batch 2",
            P1_P2_BATCHES[(week_num + 1) % 3],
            "P1/P2 Batch 2",
            "P1/P2 Batch 3 Friday",
        ),
        3: ("P0", P0_PUBLISHERS, "P0 publishers", "P1/P2 Batch 3 Friday"),
        4: (
            "P1/P2 Batch 3",
            P1_P2_BATCHES[(week_num + 2) % 3],
            "P1/P2 Batch 3",
            "P0 publishers Monday",
        ),
    }

    if weekday not in schedule:
        return None, None, None, None

    return schedule[weekday]
