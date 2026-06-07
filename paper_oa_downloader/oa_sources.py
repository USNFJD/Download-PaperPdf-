from __future__ import annotations

import itertools
from dataclasses import dataclass


@dataclass(frozen=True)
class OAPublisher:
    name: str
    region: str
    focus: str
    examples: str


FAMOUS_OA_PUBLISHERS: list[OAPublisher] = [
    OAPublisher("PLOS", "US/UK", "General science and life science", "PLOS ONE, PLOS Biology"),
    OAPublisher("BioMed Central / Springer Nature", "UK/DE", "Medicine, life science, OA journals", "BMC Medicine, Scientific Reports"),
    OAPublisher("SpringerOpen", "DE/UK", "Medicine, science, engineering, humanities", "EPJ Data Science, SpringerOpen journals"),
    OAPublisher("Nature Portfolio Open Access", "UK/DE", "High-impact multidisciplinary and life science OA", "Nature Communications, Scientific Reports"),
    OAPublisher("MDPI", "CH", "Broad-scope OA journals", "Sensors, Molecules, Sustainability"),
    OAPublisher("Frontiers", "CH", "Life science, medicine, psychology, engineering", "Frontiers in Immunology"),
    OAPublisher("Hindawi / Wiley", "UK/US", "Broad-scope OA journals", "Computational Intelligence and Neuroscience"),
    OAPublisher("Elsevier Open Access", "NL", "General, medicine, engineering", "Heliyon, Cell Reports"),
    OAPublisher("Cell Press Open Access", "US/NL", "Life science, medicine, interdisciplinary science", "Cell Reports, iScience, STAR Protocols"),
    OAPublisher("The Lancet Discovery Science", "UK/NL", "Clinical and global health OA journals", "eClinicalMedicine, eBioMedicine"),
    OAPublisher("Taylor & Francis Open / Cogent", "UK", "General, humanities, social science, engineering", "Cogent Engineering"),
    OAPublisher("Dove Medical Press / Taylor & Francis", "NZ/UK", "Medicine, clinical science, pharmacology", "International Journal of Nanomedicine"),
    OAPublisher("SAGE Open", "US/UK", "Social science and medicine", "SAGE Open Medicine"),
    OAPublisher("De Gruyter Open", "DE", "General, engineering, humanities", "Open Chemistry"),
    OAPublisher("Sciendo / De Gruyter", "PL/DE", "Central European journals, humanities, science", "Open Medicine, Acta Facultatis"),
    OAPublisher("Copernicus Publications", "DE", "Earth science and environment", "Atmospheric Chemistry and Physics"),
    OAPublisher("eLife Sciences", "UK/US", "Life science and medicine", "eLife"),
    OAPublisher("PeerJ", "US/UK", "Life science, medicine, computer science", "PeerJ, PeerJ Computer Science"),
    OAPublisher("F1000Research / Taylor & Francis", "UK", "Open research publishing, medicine, life science", "F1000Research, Gates Open Research"),
    OAPublisher("Ubiquity Press", "UK", "University-led OA journals and books", "Journal of Open Research Software"),
    OAPublisher("Open Library of Humanities", "UK", "Humanities and social science diamond OA", "OLH, 19: Interdisciplinary Studies"),
    OAPublisher("Open Book Publishers", "UK", "OA monographs and humanities books", "Open access scholarly books"),
    OAPublisher("IEEE Access", "US", "Engineering, computing, electronics", "IEEE Access"),
    OAPublisher("ACS Publications OA", "US", "Chemistry and materials", "ACS Omega"),
    OAPublisher("RSC Open Access", "UK", "Chemistry, materials, chemical biology", "Chemical Science, RSC Advances"),
    OAPublisher("AIP Publishing Open Access", "US", "Physics, materials, applied science", "APL Bioengineering, APL Materials"),
    OAPublisher("IOP Publishing Open Access", "UK", "Physics, materials, astronomy", "JPhys Materials, Machine Learning: Science and Technology"),
    OAPublisher("Optica Publishing Group OA", "US", "Optics and photonics", "Optics Express, Biomedical Optics Express"),
    OAPublisher("SPIE Open Access", "US", "Optics, photonics, imaging", "Neurophotonics, Journal of Medical Imaging"),
    OAPublisher("Royal Society Publishing OA", "UK", "General and life science", "Royal Society Open Science"),
    OAPublisher("BMJ Open", "UK", "Medicine and public health", "BMJ Open"),
    OAPublisher("BMJ Open Access Journals", "UK", "Clinical medicine, public health, evidence-based medicine", "BMJ Global Health, BMJ Open Science"),
    OAPublisher("JMIR Publications", "CA", "Digital health and medical informatics", "Journal of Medical Internet Research"),
    OAPublisher("Cureus / Springer Nature", "US", "Clinical medicine and case reports", "Cureus"),
    OAPublisher("Karger Open Access", "CH", "Medicine and biomedical science", "Medical Cannabis and Cannabinoids"),
    OAPublisher("Wolters Kluwer Medknow", "IN/US", "Medicine and clinical journals", "Indian Journal of Ophthalmology"),
    OAPublisher("Ivyspring International Publisher", "AU", "Biomedical science, oncology, translational medicine", "Theranostics, International Journal of Biological Sciences"),
    OAPublisher("Spandidos Publications OA", "GR/UK", "Oncology, molecular medicine", "Oncology Letters, Molecular Medicine Reports"),
    OAPublisher("Oxford University Press OA", "UK", "General, medicine, humanities", "NAR, Oxford Open journals"),
    OAPublisher("Cambridge University Press OA", "UK", "General, humanities, science", "Cambridge Open Engage journals"),
    OAPublisher("MIT Press Direct OA", "US", "Science, technology, humanities, social science", "Open Mind, Network Neuroscience"),
    OAPublisher("University of California Press OA", "US", "Science, humanities, social science", "Elementa, Collabra: Psychology"),
    OAPublisher("Canadian Science Publishing OA", "CA", "Natural science, biology, environment", "FACETS"),
    OAPublisher("CSIRO Publishing Open Access", "AU", "Agriculture, environment, biology", "Microbiology Australia, Pacific Conservation Biology"),
    OAPublisher("Tech Science Press", "US", "Engineering, computer science, biomedicine", "CMES, Computers Materials & Continua"),
    OAPublisher("JMIRx and Medicine 2.0 family", "CA", "Digital medicine, health informatics, rapid reviews", "JMIRx Med, JMIR mHealth and uHealth"),
    OAPublisher("Scielo", "BR/Global South", "Latin American and global south OA journals", "SciELO Brazil, SciELO Public Health"),
    OAPublisher("Redalyc / AmeliCA", "MX/Latin America", "Diamond OA journals in Latin America", "Redalyc indexed journals"),
    OAPublisher("J-STAGE", "JP", "Japanese scholarly society OA platform", "J-STAGE open journals"),
    OAPublisher("KoreaMed Synapse / KAMJE", "KR", "Korean medical and biomedical OA journals", "Journal of Korean Medical Science"),
    OAPublisher("African Journals Online", "ZA/Africa", "African scholarly journals with OA coverage", "AJOL open access journals"),
    OAPublisher("Egyptian Knowledge Bank / SpringerOpen", "EG", "Egyptian society and university OA journals", "Egyptian Journal of Radiology and Nuclear Medicine"),
    OAPublisher("AOSIS", "ZA", "African open access scholarly journals", "HTS Teologiese Studies, SA Journal of Science"),
    OAPublisher("PAGEPress", "IT", "Medicine, biology, agriculture", "Journal of Biological Research"),
    OAPublisher("Edorium Journals", "UK/IN", "Medicine and case reports", "International Journal of Case Reports and Images"),
    OAPublisher("BMC Series Journals", "UK/DE", "Subject-specific biomedical OA journals", "BMC Cancer, BMC Genomics, BMC Public Health"),
    OAPublisher("Scientific Reports", "UK/DE", "Multidisciplinary OA mega-journal", "Scientific Reports"),
    OAPublisher("Heliyon", "NL/US", "Multidisciplinary OA mega-journal", "Heliyon"),
    OAPublisher("Communications Journals", "UK/DE", "Specialist OA journals from Nature Portfolio", "Communications Biology, Communications Medicine"),
    OAPublisher("Oxford Open Journals", "UK", "OUP fully OA journal series", "Oxford Open Immunology, Oxford Open Climate Change"),
    OAPublisher("Cambridge Prisms", "UK", "Interdisciplinary OA journals", "Cambridge Prisms: Global Mental Health"),
    OAPublisher("Wiley Open Access", "US/UK", "Medicine, life science, environment, engineering", "Ecology and Evolution, Clinical Case Reports"),
    OAPublisher("Emerald Open Research", "UK", "Open research platform, social science and policy", "Emerald Open Research"),
    OAPublisher("Brill Open", "NL", "Humanities, social science, law", "Brill Open journals and books"),
    OAPublisher("IOS Press Open Library", "NL", "Computer science, medicine, neuroscience", "Journal of Alzheimer's Disease Reports"),
]

OA_PUBLISHER_SEARCH_TERMS: list[str] = [
    "Multidisciplinary Digital Publishing Institute",
    "Frontiers Media",
    "Public Library of Science",
    "BioMed Central",
    "Hindawi Publishing Corporation",
    "Springer Nature",
    "Elsevier",
    "Cell Press",
    "Taylor & Francis",
    "SAGE Publishing",
    "De Gruyter",
    "Copernicus Publications",
    "eLife Sciences",
    "PeerJ",
    "Ubiquity Press",
    "IEEE",
    "American Chemical Society",
    "Royal Society of Chemistry",
    "IOP Publishing",
    "Optica Publishing Group",
    "BMJ",
    "JMIR Publications",
    "Oxford University Press",
    "Cambridge University Press",
    "Wiley",
    "Tech Science Press",
    "AOSIS",
]


def clean_keywords(keywords: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in keywords:
        value = item.strip()
        key = value.lower()
        if value and key not in seen:
            cleaned.append(value)
            seen.add(key)
    return cleaned[:3]


def pair_queries(keywords: list[str]) -> list[str]:
    cleaned = clean_keywords(keywords)
    return [" ".join(combo) for combo in itertools.combinations(cleaned, 2)]


def single_queries(keywords: list[str]) -> list[str]:
    return clean_keywords(keywords)
