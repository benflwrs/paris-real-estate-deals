import os

import pandas as pd
import pytest

from pipeline.dvf_import import parse_dvf_csv

FIXTURE_CSV = os.path.join(os.path.dirname(__file__), "fixtures", "dvf_sample.csv")


@pytest.fixture(scope="module")
def sample_csv_bytes():
    with open(FIXTURE_CSV, "rb") as f:
        return f.read()


def test_parse_filters_to_appartement_maison(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert set(df["property_type"].unique()) <= {"Appartement", "Maison"}


def test_parse_drops_rows_missing_required_fields(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert df["price"].isna().sum() == 0
    assert df["surface_m2"].isna().sum() == 0
    assert df["longitude"].isna().sum() == 0
    assert df["latitude"].isna().sum() == 0


def test_parse_drops_zero_or_negative_price_and_surface(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert (df["price"] > 0).all()
    assert (df["surface_m2"] > 0).all()


def test_parse_output_columns(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    expected_cols = {
        "mutation_date", "price", "surface_m2", "rooms",
        "property_type", "insee_code", "longitude", "latitude",
    }
    assert expected_cols == set(df.columns)


def test_parse_excludes_dependance_and_local_commercial(sample_csv_bytes):
    df = parse_dvf_csv(sample_csv_bytes)
    assert "Dépendance" not in df["property_type"].values
    assert "Local industriel. commercial ou assimilé" not in df["property_type"].values


def test_parse_drops_tiny_surface_outliers():
    import io

    header = "id_mutation,date_mutation,numero_disposition,nature_mutation,valeur_fonciere,adresse_numero,adresse_suffixe,adresse_nom_voie,adresse_code_voie,code_postal,code_commune,nom_commune,code_departement,ancien_code_commune,ancien_nom_commune,id_parcelle,ancien_id_parcelle,numero_volume,lot1_numero,lot1_surface_carrez,lot2_numero,lot2_surface_carrez,lot3_numero,lot3_surface_carrez,lot4_numero,lot4_surface_carrez,lot5_numero,lot5_surface_carrez,nombre_lots,code_type_local,type_local,surface_reelle_bati,nombre_pieces_principales,code_nature_culture,nature_culture,code_nature_culture_speciale,nature_culture_speciale,surface_terrain,longitude,latitude\n"
    # A normal row plus a bogus 1m2 "mis-split lot" row that would otherwise
    # produce an absurd price/m2 and poison the value model.
    normal_row = "1,2024-01-01,1,Vente,500000,1,,RUE X,1,75011,75111,Paris,75,,,ID1,,,,,,,,,,,,,2,2,Appartement,50,3,,,,,,2.37,48.86\n"
    bogus_row = "2,2024-01-01,1,Vente,300000,1,,RUE X,1,75011,75111,Paris,75,,,ID2,,,,,,,,,,,,,2,2,Appartement,1,0,,,,,,2.37,48.86\n"
    csv_bytes = (header + normal_row + bogus_row).encode("utf-8")

    df = parse_dvf_csv(csv_bytes)
    assert len(df) == 1
    assert df.iloc[0]["surface_m2"] == 50


def test_parse_drops_extreme_price_per_m2_outliers():
    header = "id_mutation,date_mutation,numero_disposition,nature_mutation,valeur_fonciere,adresse_numero,adresse_suffixe,adresse_nom_voie,adresse_code_voie,code_postal,code_commune,nom_commune,code_departement,ancien_code_commune,ancien_nom_commune,id_parcelle,ancien_id_parcelle,numero_volume,lot1_numero,lot1_surface_carrez,lot2_numero,lot2_surface_carrez,lot3_numero,lot3_surface_carrez,lot4_numero,lot4_surface_carrez,lot5_numero,lot5_surface_carrez,nombre_lots,code_type_local,type_local,surface_reelle_bati,nombre_pieces_principales,code_nature_culture,nature_culture,code_nature_culture_speciale,nature_culture_speciale,surface_terrain,longitude,latitude\n"
    normal_row = "1,2024-01-01,1,Vente,500000,1,,RUE X,1,75011,75111,Paris,75,,,ID1,,,,,,,,,,,,,2,2,Appartement,50,3,,,,,,2.37,48.86\n"
    # 10m2 sold for 3,000,000 => 300k/m2, an implausible partial-ownership/typo outlier.
    extreme_row = "2,2024-01-01,1,Vente,3000000,1,,RUE X,1,75011,75111,Paris,75,,,ID2,,,,,,,,,,,,,2,2,Appartement,10,1,,,,,,2.37,48.86\n"
    csv_bytes = (header + normal_row + extreme_row).encode("utf-8")

    df = parse_dvf_csv(csv_bytes)
    assert len(df) == 1
    assert df.iloc[0]["price"] == 500000
