"""
CSV export functionality.
"""
import pandas as pd
from pathlib import Path
from typing import List

from src.schemas import CompanyData, OutOfScopeRecord, LowConfidenceRecord
from src.logger import get_logger

logger = get_logger(__name__)


class CSVExporter:
    """Export scraped data to CSV files."""

    def __init__(self, output_dir: str = "data/output"):
        """
        Initialize exporter.

        Args:
            output_dir: Directory for output files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_results(self, data: List[CompanyData], filename: str = "results.csv") -> str:
        """
        Export successful extractions to CSV.

        Args:
            data: List of CompanyData
            filename: Output filename

        Returns:
            Path to exported file or empty string if failed
        """
        if not data:
            logger.warning("No data to export to results.csv")
            return ""

        # Convert to flat dicts
        rows = [self._company_to_dict(company) for company in data]

        # Create DataFrame
        df = pd.DataFrame(rows)

        # Save to CSV
        output_path = self.output_dir / filename
        df.to_csv(output_path, index=False, quoting=1)  # QUOTE_ALL

        logger.info(f"Exported {len(rows)} records to {output_path}")
        return str(output_path)

    def export_out_of_scope(self, data: List[OutOfScopeRecord], filename: str = "out_of_scope_urls.csv") -> str:
        """
        Export out-of-scope URLs to CSV.

        Args:
            data: List of OutOfScopeRecord
            filename: Output filename

        Returns:
            Path to exported file or empty string if failed
        """
        if not data:
            logger.info("No out-of-scope URLs to export")
            return ""

        rows = [record.model_dump() for record in data]

        df = pd.DataFrame(rows)
        output_path = self.output_dir / filename
        df.to_csv(output_path, index=False)

        logger.info(f"Exported {len(rows)} out-of-scope URLs to {output_path}")
        return str(output_path)

    def export_low_confidence(self, data: List[LowConfidenceRecord], filename: str = "low_confidence_urls.csv") -> str:
        """
        Export low-confidence records to CSV.

        Args:
            data: List of LowConfidenceRecord
            filename: Output filename

        Returns:
            Path to exported file or empty string if failed
        """
        if not data:
            logger.info("No low-confidence records to export")
            return ""

        rows = [record.model_dump() for record in data]

        df = pd.DataFrame(rows)
        output_path = self.output_dir / filename
        df.to_csv(output_path, index=False)

        logger.info(f"Exported {len(rows)} low-confidence records to {output_path}")
        return str(output_path)

    def export_broken_urls(self, urls: List[str], filename: str = "broken_urls.txt") -> str:
        """
        Export broken URLs to text file.

        Args:
            urls: List of broken URLs
            filename: Output filename

        Returns:
            Path to exported file or empty string if failed
        """
        if not urls:
            logger.info("No broken URLs to export")
            return ""

        output_path = self.output_dir / filename
        with open(output_path, 'w') as f:
            for url in urls:
                f.write(f"{url}\n")

        logger.info(f"Exported {len(urls)} broken URLs to {output_path}")
        return str(output_path)

    def _company_to_dict(self, company: CompanyData) -> dict:
        """
        Convert CompanyData to flat dict for CSV.

        Args:
            company: CompanyData instance

        Returns:
            Flat dictionary with all fields
        """
        result = {
            "company_name": company.company_name or "",
            "company_url": company.company_url,
            "office_phone": company.office_phone or "",
            "office_email": company.office_email or "",
            "associated_emails": "; ".join(company.associated_emails),
            "associated_mobile_numbers": "; ".join(company.associated_mobile_numbers),
            "associated_info": company.associated_info,
            "associated_location": company.associated_location or "",
            "organisational_structure": company.organisational_structure,
            "team": company.team,
            "description": company.description,
            "edited_description": company.edited_description,
            "business_segment": company.business_segment,
            "confidence_score": company.confidence_score,
        }

        # Add up to 3 decision makers
        for i in range(3):
            if i < len(company.decision_makers):
                dm = company.decision_makers[i]
                result[f"dm_{i+1}_name"] = dm.name or ""
                result[f"dm_{i+1}_title"] = dm.title or ""
                result[f"dm_{i+1}_decision_maker_summary"] = dm.decision_maker_summary or ""
                result[f"dm_{i+1}_phone_office"] = dm.phone_office or ""
                result[f"dm_{i+1}_phone_mobile"] = dm.phone_mobile or ""
                result[f"dm_{i+1}_phone_direct"] = dm.phone_direct or ""
                result[f"dm_{i+1}_email"] = dm.email or ""
                result[f"dm_{i+1}_linkedin"] = dm.linkedin or ""
            else:
                result[f"dm_{i+1}_name"] = ""
                result[f"dm_{i+1}_title"] = ""
                result[f"dm_{i+1}_decision_maker_summary"] = ""
                result[f"dm_{i+1}_phone_office"] = ""
                result[f"dm_{i+1}_phone_mobile"] = ""
                result[f"dm_{i+1}_phone_direct"] = ""
                result[f"dm_{i+1}_email"] = ""
                result[f"dm_{i+1}_linkedin"] = ""

        return result
