"""
pitch_engine.py — Moteur de Copywriting Commercial & Psychologie de Vente d'Élite (DCE-BF).
Basé sur les travaux de Chris Voss, Robert Cialdini, Oren Klaff, Josh Braun, Jeremy Miner (NEPQ)
et adapté aux codes culturels des affaires et de WhatsApp au Burkina Faso (Ouagadougou, Bobo-Dioulasso, etc.).
"""

from __future__ import annotations
import urllib.parse
from typing import Any, Dict, Union


# ---------------------------------------------------------------------------
# 1. MATRICE ÉCONOMIQUE DES 16 SECTEURS B2B AU BURKINA FASO (MÉTRIQUES FCFA)
# ---------------------------------------------------------------------------

SECTOR_METRICS: Dict[str, Dict[str, str]] = {
    "hotel": {
        "label": "Hôtels, Auberges & Résidences",
        "salutation": "Monsieur le Gérant",
        "client_term": "voyageurs d'affaires et touristes",
        "loss_desc": "nuitées réservées directement sans commissions d'agences",
        "loss_amount": "80 000 à 250 000 FCFA par semaine",
        "value_prop": "un système de réservation de chambres instantané avec galerie photo relié directement à votre WhatsApp",
        "competitor_example": "les résidences et hôtels de votre zone",
    },
    "sante": {
        "label": "Cliniques, Cabinets & Labos",
        "salutation": "Docteur",
        "client_term": "patients recherchant une prise en charge urgente",
        "loss_desc": "consultations spécialisées et bilans médicaux orientés vers d'autres centres",
        "loss_amount": "150 000 à 500 000 FCFA par mois de soins",
        "value_prop": "une orientation médicale claire, localisation Google Maps et prise de rendez-vous fluide sur WhatsApp",
        "competitor_example": "les cabinets médicaux et polycliniques de la place",
    },
    "pharmacie": {
        "label": "Pharmacies & Dépôts",
        "salutation": "Docteur",
        "client_term": "clients cherchant une officine de garde ou un produit spécifique",
        "loss_desc": "ordonnances et produits de santé achetés ailleurs faute d'information en ligne",
        "loss_amount": "60 000 à 200 000 FCFA par semaine",
        "value_prop": "une fiche Google Maps Top 3 avec bouton d'appel direct et vérification des disponibilités sur WhatsApp",
        "competitor_example": "les officines bien référencées du quartier",
    },
    "resto": {
        "label": "Restaurants, Cafés & Maquis VIP",
        "salutation": "Monsieur le Promoteur",
        "client_term": "amateurs de bonne table et déjeuners d'affaires",
        "loss_desc": "tables non réservées à midi et commandes traiteurs parties chez vos voisins",
        "loss_amount": "120 000 à 450 000 FCFA par week-end",
        "value_prop": "un menu digital interactif consultable sur smartphone avec commande de table WhatsApp en 1 clic",
        "competitor_example": "les espaces et restaurants les plus prisés de la ville",
    },
    "btp": {
        "label": "BTP, Quincailleries & Solaire",
        "salutation": "Monsieur le Directeur",
        "client_term": "entrepreneurs, particuliers et maîtres d'ouvrage",
        "loss_desc": "demandes de devis matériaux, installations solaires ou forages",
        "loss_amount": "500 000 à 2 500 000 FCFA par marché manqué",
        "value_prop": "un catalogue technique clair avec demande de devis instantanée transmise sur WhatsApp",
        "competitor_example": "les quincailleries modernes et entreprises BTP bien positionnées",
    },
    "auto": {
        "label": "Auto, Location Véhicules & Garages",
        "salutation": "Monsieur le Responsable",
        "client_term": "automobilistes en panne ou clients cherchant un véhicule de location",
        "loss_desc": "locations de véhicules et réparations mécaniques urgentes",
        "loss_amount": "100 000 à 350 000 FCFA par semaine de location",
        "value_prop": "votre parc automobile et vos services repérés immédiatement sur Google Maps et WhatsApp",
        "competitor_example": "les loueurs et garages les mieux notés de la zone",
    },
    "commerce": {
        "label": "Supermarchés, Boutiques & Mode",
        "salutation": "Cher Promoteur",
        "client_term": "acheteurs cherchant des articles de mode, équipement ou alimentation",
        "loss_desc": "visiteurs repartis sans acheter faute de catalogue lisible ou de relance",
        "loss_amount": "150 000 à 600 000 FCFA de chiffre d'affaires mensuel",
        "value_prop": "une vitrine e-commerce fluide couplée à la publicité Meta Ads et aux commandes WhatsApp",
        "competitor_example": "les boutiques tendance qui captent l'attention sur Facebook et Google",
    },
    "finance": {
        "label": "Assurances, Microfinance & Cabinets",
        "salutation": "Monsieur le Directeur",
        "client_term": "professionnels et particuliers en quête de solutions de crédit ou d'assurance",
        "loss_desc": "dossiers de financement, souscriptions d'assurance ou contrats d'expertise comptable",
        "loss_amount": "300 000 à 1 500 000 FCFA de commissions",
        "value_prop": "un simulateur de devis en ligne et un canal de prise de contact sécurisé pour vos conseillers",
        "competitor_example": "les cabinets d'assurance et institutions financières majeures",
    },
    "education": {
        "label": "Universités, Écoles & Formation",
        "salutation": "Monsieur le Fondateur",
        "client_term": "étudiants et parents d'élèves pour les inscriptions",
        "loss_desc": "inscriptions de rentrée académique ou formations continues",
        "loss_amount": "400 000 à 1 800 000 FCFA par promotion d'étudiants",
        "value_prop": "la plaquette des filières téléchargeable en ligne et pré-inscription directe sur WhatsApp",
        "competitor_example": "les instituts et universités privées en tête des recherches web",
    },
    "voyage": {
        "label": "Agences de Voyage & Immobilier",
        "salutation": "Cher Directeur",
        "client_term": "voyageurs pour billets d'avion et clients en quête de logements",
        "loss_desc": "commissions sur vols et mandats de location ou vente immobilière",
        "loss_amount": "150 000 à 750 000 FCFA par transaction manquée",
        "value_prop": "vos offres de vols et annonces immobilières reliées à une ligne WhatsApp commerciale ultra-réactive",
        "competitor_example": "les agences de voyages et cabinets immobiliers leaders",
    },
    "agri": {
        "label": "Agriculture, Élevage & Agroalimentaire",
        "salutation": "Cher Promoteur",
        "client_term": "grossistes, restaurateurs et acheteurs d'intrants",
        "loss_desc": "commandes en gros de volailles, céréales, bétail ou semences",
        "loss_amount": "250 000 à 1 200 000 FCFA par commande gros",
        "value_prop": "un bon de commande clair et un canal d'expédition direct sur WhatsApp pour grossistes",
        "competitor_example": "les fermes et distributeurs agroalimentaires connectés",
    },
    "beaute": {
        "label": "Beauté, Coiffure & Cosmétique",
        "salutation": "Madame la Directrice",
        "client_term": "clientes et clients recherchant des soins de standing",
        "loss_desc": "créneaux de soins non réservés en semaine et ventes de cosmétiques",
        "loss_amount": "50 000 à 200 000 FCFA par semaine de créneaux",
        "value_prop": "la grille des tarifs et la réservation de créneaux en 2 clics sur smartphone",
        "competitor_example": "les salons VIP et instituts les plus visibles sur les réseaux",
    },
    "industrie": {
        "label": "Industrie, Usines & Manufactures",
        "salutation": "Monsieur le Directeur",
        "client_term": "distributeurs, acheteurs industriels et entreprises",
        "loss_desc": "contrats de fourniture industrielle ou partenariats grossistes",
        "loss_amount": "1 000 000 à 5 000 000 FCFA par contrat B2B",
        "value_prop": "une vitrine institutionnelle de haute tenue avec fiches techniques et contact direct",
        "competitor_example": "les usines et manufactures de référence dans le pays",
    },
    "event": {
        "label": "Événementiel, Loisirs & Nuit",
        "salutation": "Cher Promoteur",
        "client_term": "organisateurs d'événements, mariages, conférences et soirées",
        "loss_desc": "dates de week-end sans location de salle ou réservation VIP",
        "loss_amount": "300 000 à 1 200 000 FCFA par date non réservée",
        "value_prop": "la visite virtuelle de votre espace avec planning des disponibilités sur WhatsApp",
        "competitor_example": "les salles de fêtes et espaces réceptifs les plus demandés",
    },
    "securite": {
        "label": "Sécurité Privée & Gardiennage",
        "salutation": "Monsieur le Directeur Général",
        "client_term": "sociétés, ONG, résidences et chantiers recherchant un gardiennage",
        "loss_desc": "contrats de surveillance et installations de vidéosurveillance",
        "loss_amount": "400 000 à 2 000 000 FCFA par contrat annuel",
        "value_prop": "une plaquette de sécurité certifiée et un chiffrage rapide envoyé par WhatsApp",
        "competitor_example": "les sociétés de gardiennage recommandées sur le secteur",
    },
    "entreprise": {
        "label": "Bureaux, Sociétés IT, Conseil & ONG",
        "salutation": "Monsieur le Directeur",
        "client_term": "dirigeants, institutions et partenaires d'affaires",
        "loss_desc": "missions d'expertise, audits ou prestations de service B2B",
        "loss_amount": "500 000 à 3 000 000 FCFA par mission",
        "value_prop": "un portfolio professionnel valorisant votre expertise avec prise de contact rapide",
        "competitor_example": "les cabinets conseils et sociétés réputées à Ouagadougou",
    },
}


# ---------------------------------------------------------------------------
# 2. MOTEUR ALGORITHMIQUE DE GÉNÉRATION DYNAMIQUE
# ---------------------------------------------------------------------------

class DynamicPitchEngine:
    VALID_OFFERS = {"WEB", "ADS", "MAPS"}
    VALID_ANGLES = {"voss", "diagnostic", "reciprocity", "social_proof"}
    VALID_STAGES = {"j0", "j2", "j5", "j8"}

    @classmethod
    def generate(
        cls,
        lead: Union[Dict[str, Any], Any],
        offer: str = "WEB",
        angle: str = "voss",
        stage: str = "j0",
    ) -> str:
        """
        Assemble un message hautement personnalisé selon le profil exact de l'entreprise.
        Compatible avec les objets BusinessLead et les dictionnaires JSON.
        """
        # Normalisation des paramètres de contrôle
        offer = offer.upper() if offer and offer.upper() in cls.VALID_OFFERS else "WEB"
        angle = angle.lower() if angle and angle.lower() in cls.VALID_ANGLES else "voss"
        stage = stage.lower() if stage and stage.lower() in cls.VALID_STAGES else "j0"

        # Extraction des métadonnées du prospect
        if isinstance(lead, dict):
            name = (lead.get("name") or "l'entreprise").strip()
            city = lead.get("city") or "Burkina Faso"
            raw_neigh = (lead.get("neighborhood") or "").strip()
            sector_key = (lead.get("sector_key") or "entreprise").strip().lower()
            status = lead.get("status") or lead.get("audit_status") or "SANS_SITE"
            issues = lead.get("issues") or []
            website = lead.get("website") or ""
            response_ms = lead.get("response_ms") or 0
            has_pixel = bool(lead.get("has_fb_pixel"))
        else:
            name = getattr(lead, "name", "l'entreprise").strip()
            city = getattr(lead, "city", "Burkina Faso")
            raw_neigh = getattr(lead, "neighborhood", "").strip()
            sector_key = getattr(lead, "sector_key", "entreprise").strip().lower()
            status = getattr(lead, "audit_status", getattr(lead, "status", "SANS_SITE"))
            issues = getattr(lead, "issues", [])
            website = getattr(lead, "website", "")
            response_ms = getattr(lead, "response_ms", 0)
            has_pixel = bool(getattr(lead, "has_fb_pixel", False))

        if city == "Audit Direct":
            city = "Burkina Faso"

        neighborhood = (
            f"vers {raw_neigh}"
            if raw_neigh and raw_neigh not in ("Quartier non précisé", "Saisie manuelle")
            else f"à {city}"
        )

        metrics = SECTOR_METRICS.get(sector_key, SECTOR_METRICS["entreprise"])
        salutation_title = metrics.get("salutation", "Monsieur le Responsable")

        # CADENCE TEMPORELLE DE RELANCE (J+2, J+5, J+8)
        if stage == "j2":
            return cls._render_j2(name, city, salutation_title, offer)
        elif stage == "j5":
            return cls._render_j5(name, city, salutation_title, metrics, offer)
        elif stage == "j8":
            return cls._render_j8(name, city, offer)

        # PREMIER CONTACT J0 STRUCTURÉ EN 5 MODULES D'ÉLITE
        hook = cls._build_hook(name, city, salutation_title)
        observation = cls._build_observation(name, city, neighborhood, website, status, issues, response_ms, has_pixel, offer, angle)
        impact = cls._build_impact(metrics, status, offer, angle)
        value = cls._build_value(metrics, offer, status)
        cta = cls._build_cta(angle, offer)

        return f"{hook}\n\n{observation} {impact}\n\n{value}.\n\n👉 {cta}"

    @classmethod
    def _build_hook(cls, name: str, city: str, salutation_title: str) -> str:
        return f"Bonjour {salutation_title} ({name} • {city}),"

    @classmethod
    def _build_observation(
        cls,
        name: str,
        city: str,
        neighborhood: str,
        website: str,
        status: str,
        issues: list,
        response_ms: int,
        has_pixel: bool,
        offer: str,
        angle: str,
    ) -> str:
        site_str = website or "votre site"
        flaws = [i for i in issues if "Pixel" not in i and "Analytics" not in i]
        flaw_text = flaws[0].lower() if flaws else "un temps de réponse ralenti sur téléphone"

        if offer == "WEB":
            if status == "SITE_MORT":
                return f"En voulant consulter votre site web ({site_str}) {neighborhood}, j'ai constaté qu'il est en panne et affiche une page d'erreur technique (serveur hors-ligne ou certificat expiré)."
            if status == "SITE_FAIBLE":
                ms_text = f" ({response_ms} ms mesurés sur smartphone)" if response_ms > 0 else ""
                return f"En visitant votre site ({site_str}) depuis mon téléphone {neighborhood}, j'ai relevé des défauts techniques{ms_text} liés à {flaw_text}."
            if status == "FACEBOOK_SEUL":
                return f"Votre page Facebook {neighborhood} fonctionne, mais vous ne disposez pas encore d'un site web officiel professionnel à votre nom."
            if status == "SANS_SITE":
                return f"En recherchant vos services {neighborhood}, j'ai remarqué que votre établissement ne dispose pas encore de vitrine digitale officielle."
            return f"Votre site web ({site_str}) est bien en ligne, mais il lui manque un tunnel de conversion moderne vers WhatsApp."

        elif offer == "ADS":
            if not has_pixel:
                return f"En analysant votre dispositif digital {neighborhood}, j'ai noté qu'aucun Pixel Meta / Pixel Facebook n'est configuré : vos visiteurs repartent sans que vous puissiez les relancer par publicité sponsorisée ou les recibler."
            return f"À {neighborhood}, des centaines de clients potentiels consultent vos réseaux sociaux chaque semaine sans être convertis directement en clients."

        else:  # MAPS
            return f"Lorsqu'un client recherche vos services {neighborhood} ou à {city} directement sur Google Maps depuis son téléphone,"

    @classmethod
    def _build_impact(cls, metrics: Dict[str, str], status: str, offer: str, angle: str) -> str:
        loss_desc = metrics["loss_desc"]
        loss_amount = metrics["loss_amount"]
        competitors = metrics["competitor_example"]
        client_term = metrics["client_term"]

        if angle == "diagnostic":
            return f"Conséquence directe : plus de 60% des visiteurs mobiles quittent la page sans appeler, ce qui représente une perte estimée à {loss_amount} en {loss_desc}."
        elif angle == "social_proof":
            return f"Pendant ce temps, {competitors} captent l'essentiel de ces prospects qualifiés, vous privant d'environ {loss_amount} d'activité."
        elif angle == "reciprocity":
            return f"Beaucoup d'entreprises perdent environ {loss_amount} simplement à cause de frictions évitables lors de la première interaction sur mobile."
        else:  # voss
            return f"Vos futurs {client_term} hésitent et partent avant de vous contacter, ce qui peut représenter une opportunité manquée de {loss_amount}."

    @classmethod
    def _build_value(cls, metrics: Dict[str, str], offer: str, status: str) -> str:
        if offer == "WEB":
            if status in ("SITE_MORT", "SITE_FAIBLE"):
                return f"Je vous propose de corriger cette panne et d'effectuer une refonte moderne de votre vitrine en 72h avec un affichage ultra-rapide en 1 seconde et {metrics['value_prop']}"
            return f"Nous vous créons une vitrine officielle moderne, rapide sur smartphone et reliée directement à {metrics['value_prop']}"
        elif offer == "ADS":
            return f"Nous pilotons vos campagnes de publicité sponsorisée Meta Ads et configurons le pixel de retargeting pour recibler ces visiteurs et les transformer en discussions qualifiées sur votre WhatsApp"
        else:  # MAPS
            return f"Nous positionnons votre établissement dans le Top 3 Google Maps avec optimisation SEO local et gestion des avis clients pour capter en priorité tous les appels et visites de votre zone"

    @classmethod
    def _build_cta(cls, angle: str, offer: str) -> str:
        if angle == "voss":
            return "Seriez-vous contre l'idée que je vous envoie un aperçu vidéo de 60 secondes sans aucun engagement ?"
        elif angle == "diagnostic":
            return "Avez-vous 2 minutes pour que je vous transmette les détails du test technique directement sur WhatsApp ?"
        elif angle == "reciprocity":
            return "J'ai déjà préparé un modèle adapté à votre activité : souhaitez-vous que je vous le transmette gratuitement ici ?"
        else:  # social_proof
            return "Serait-ce déraisonnable de faire un point de 5 minutes pour comparer votre visibilité avec vos concurrents locaux ?"

    @classmethod
    def _render_j2(cls, name: str, city: str, salutation_title: str, offer: str) -> str:
        return (
            f"Bonjour {salutation_title} ({name}),\n\n"
            f"Je me permets de faire remonter mon message précédent concernant le renforcement de votre visibilité {offer} à {city}.\n\n"
            f"Avez-vous eu l'opportunité d'en prendre connaissance, ou préférez-vous que nous refassions un point la semaine prochaine ?\n\n"
            f"👉 Un simple « Oui » ou « Plus tard » me suffit pour adapter mon suivi !"
        )

    @classmethod
    def _render_j5(cls, name: str, city: str, salutation_title: str, metrics: Dict[str, str], offer: str) -> str:
        return (
            f"Bonjour {salutation_title} ({name}),\n\n"
            f"Pour information, nous venons d'analyser les performances digitales du secteur « {metrics['label']} » à {city}.\n\n"
            f"Les entreprises dotées d'une présence {offer} optimisée avec tunnel WhatsApp récupèrent en moyenne {metrics['loss_amount']} de commandes supplémentaires par mois.\n\n"
            f"👉 Seriez-vous curieux de voir la simulation chiffrée pour votre établissement ?"
        )

    @classmethod
    def _render_j8(cls, name: str, city: str, offer: str) -> str:
        return (
            f"Bonjour {name},\n\n"
            f"Je suppose que l'optimisation de votre présence {offer} à {city} n'est pas votre priorité actuellement, et je le respecte tout à fait.\n\n"
            f"Je classe votre dossier pour ne plus encombrer votre messagerie. Si le sujet redevient d'actualité, vous avez mes coordonnées ici même.\n\n"
            f"Je vous souhaite plein succès dans le développement de vos activités !"
        )

    @classmethod
    def build_whatsapp_url(cls, phone_clean: str, pitch_text: str) -> str:
        digits = "".join(filter(str.isdigit, str(phone_clean or "")))
        if not digits:
            return ""
        encoded = urllib.parse.quote(pitch_text)
        return f"https://wa.me/{digits}?text={encoded}"
