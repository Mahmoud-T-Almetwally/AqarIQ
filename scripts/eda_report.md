# RealEstateAnalysis — Local Data Exploration Report
- **File:** `scrappers/unified_output.jsonl`
- **Total lines:** 12,804 (empty: 0, malformed JSON: 0)
- **Valid records:** 12,804
- **Platform breakdown:** PropertyFinder: 8,027, Nawy: 4,777

## 1. ID & URL Uniqueness (Within-File Duplicates)
- **Nawy**: 4,777 rows | unique `source_id`: 4,777 (dups: 0) | unique `source_url`: 4,777 (dups: 0)
- **PropertyFinder**: 8,027 rows | unique `source_id`: 8,027 (dups: 0) | unique `source_url`: 8,027 (dups: 0)

## 2. Field Coverage (Nulls & Empties per Platform) + Observed Python Types
| Field | Types | Nawy null / empty | PropertyFinder null / empty |
|---|---|---|---|
| `platform` | str(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `source_id` | str(8027), int(4777) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `source_url` | str(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `listing_ref` | NoneType(7563), str(5241) | 4,777 (100.0%) / 0 | 2,786 (34.7%) / 0 |
| `listed_date` | NoneType(7563), str(5241) | 4,777 (100.0%) / 0 | 2,786 (34.7%) / 0 |
| `broker_name` | str(8027), NoneType(4777) | 4,777 (100.0%) / 0 | 0 (0.0%) / 0 |
| `agent_name` | NoneType(7563), str(5241) | 4,777 (100.0%) / 0 | 2,786 (34.7%) / 0 |
| `developer_name` | NoneType(8027), str(4777) | 0 (0.0%) / 0 | 8,027 (100.0%) / 0 |
| `title_raw` | str(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `property_type` | str(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `description` | str(10018), NoneType(2786) | 0 (0.0%) / 1 | 2,786 (34.7%) / 0 |
| `size_m2` | int(12803), NoneType(1) | 1 (0.0%) / 0 | 0 (0.0%) / 0 |
| `plot_size_m2` | NoneType(10286), int(2518) | 3,717 (77.8%) / 0 | 6,569 (81.8%) / 0 |
| `bedrooms` | int(12776), NoneType(28) | 21 (0.4%) / 0 | 7 (0.1%) / 0 |
| `bathrooms` | int(12778), NoneType(26) | 21 (0.4%) / 0 | 5 (0.1%) / 0 |
| `finishing` | NoneType(7831), str(4973) | 0 (0.0%) / 0 | 7,831 (97.6%) / 0 |
| `furnished` | NoneType(8175), str(4629) | 4,777 (100.0%) / 0 | 3,398 (42.3%) / 0 |
| `delivery_year` | NoneType(8028), int(4776) | 1 (0.0%) / 0 | 8,027 (100.0%) / 0 |
| `completion_status` | str(10017), NoneType(2787) | 0 (0.0%) / 0 | 2,787 (34.7%) / 0 |
| `resale` | bool(10017), NoneType(2787) | 0 (0.0%) / 0 | 2,787 (34.7%) / 0 |
| `has_nanny_room` | bool(8357), NoneType(4447) | 0 (0.0%) / 0 | 4,447 (55.4%) / 0 |
| `features` | list(12804) | 0 (0.0%) / 1,116 | 0 (0.0%) / 481 |
| `payment_methods` | list(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 2,786 |
| `price_egp` | int(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `currency` | str(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `down_payment_egp` | NoneType(7148), int(5656) | 903 (18.9%) / 0 | 6,245 (77.8%) / 0 |
| `installment_years` | NoneType(9518), int(3286) | 1,491 (31.2%) / 0 | 8,027 (100.0%) / 0 |
| `payment_frequency` | NoneType(8992), str(3812) | 965 (20.2%) / 0 | 8,027 (100.0%) / 0 |
| `price_per_m2` | int(8027), NoneType(4777) | 4,777 (100.0%) / 0 | 0 (0.0%) / 0 |
| `governorate` | str(8027), NoneType(4777) | 4,777 (100.0%) / 0 | 0 (0.0%) / 0 |
| `city` | str(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 0 |
| `compound` | str(12704), NoneType(100) | 0 (0.0%) / 0 | 100 (1.2%) / 0 |
| `latitude` | float(9949), NoneType(2855) | 69 (1.4%) / 0 | 2,786 (34.7%) / 0 |
| `longitude` | float(9949), NoneType(2855) | 69 (1.4%) / 0 | 2,786 (34.7%) / 0 |
| `compound_geometry` | NoneType(8170), dict(4634) | 143 (3.0%) / 0 | 8,027 (100.0%) / 0 |
| `image_urls` | list(12804) | 0 (0.0%) / 0 | 0 (0.0%) / 2,786 |
| `floor_plan_urls` | list(12804) | 0 (0.0%) / 1,321 | 0 (0.0%) / 8,027 |
| `master_plan_url` | NoneType(9061), str(3743) | 1,034 (21.6%) / 0 | 8,027 (100.0%) / 0 |

## 3. Categorical Distributions (per Platform)
### `property_type`
- **Nawy** (16 distinct): `Apartment`:1781, `Villa`:536, `Chalet`:524, `Administrative`:366, `Retail`:352, `Townhouse`:336, `Twinhouse`:216, `Medical`:165, `Duplex`:154, `Studio`:103, `Office`:101, `Penthouse`:89, `Loft`:40, `Cabin`:11, `Family House`:2, `Building`:1
- **PropertyFinder** (16 distinct): `Apartment`:4018, `Villa`:1589, `Chalet`:646, `Townhouse`:638, `Twin House`:394, `Duplex`:254, `Penthouse`:224, `iVilla`:172, `Hotel Apartment`:41, `Whole Building`:15, `Roof`:15, `Palace`:6, `Land`:5, `Cabin`:5, `Bulk Sale Unit`:3, `Full Floor`:2
### `finishing`
- **Nawy** (9 distinct): `Fully Finished`:2242, `Not Finished Core Shell`:1503, `Semi Finished`:673, `Fully Finished Furnished`:293, `Flexi Finishing`:48, `Luxury`:11, `Premium `:5, `White Box`:1, `Ultra-luxury`:1
- **PropertyFinder** (2 distinct): `None`:7831, `PARTLY`:196
### `furnished`
- **Nawy** (1 distinct): `None`:4777
- **PropertyFinder** (4 distinct): `NO`:3427, `None`:3398, `YES`:642, `PARTLY`:560
### `completion_status`
- **Nawy** (2 distinct): `off_plan_primary`:3071, `ready`:1706
- **PropertyFinder** (5 distinct): `completed`:2801, `None`:2787, `off_plan_primary`:1153, `completed_primary`:1064, `off_plan`:222
### `resale`
- **Nawy** (2 distinct): `False`:3071, `True`:1706
- **PropertyFinder** (2 distinct): `False`:5240, `None`:2787
### `has_nanny_room`
- **Nawy** (2 distinct): `False`:3854, `True`:923
- **PropertyFinder** (2 distinct): `None`:4447, `True`:3580
### `currency`
- **Nawy** (2 distinct): `EGP`:4761, `USD`:16
- **PropertyFinder** (1 distinct): `EGP`:8027
### `payment_frequency`
- **Nawy** (5 distinct): `quarterly`:3489, `None`:965, `monthly`:277, `semi-annually`:33, `annually`:13
- **PropertyFinder** (1 distinct): `None`:8027
### `governorate`
- **Nawy** (1 distinct): `None`:4777
- **PropertyFinder** (11 distinct): `Cairo`:4131, `Giza`:2340, `North Coast`:580, `Red Sea`:503, `Suez`:242, `Alexandria`:182, `Qalyubia`:38, `Al Daqahlya`:4, `Matrouh`:3, `South Sainai`:2, `El Esmailia`:2
### `city`
- **Nawy** (40 distinct): `New Cairo`:891, `New Capital City`:555, `6th of October City`:469, `El Sheikh Zayed`:354, `Ras El Hekma`:340, `Ain Sokhna`:258, `Mostakbal City`:231, `6th settlement`:225, `New Zayed`:171, `Northern Expansion`:143, `Sidi Abdel Rahman`:142, `October Gardens`:136, `Al Alamein`:116, `Somabay`:93, `El Shorouk`:66, `North Investors`:66, `South Investors`:57, `New Heliopolis`:55, `Al Dabaa`:49, `New Sphinx`:47, `El Gouna`:44, `El Lotus`:38, `El-Hamam`:30, `Sidi Heneish`:27, `Golden Square`:26
- **PropertyFinder** (56 distinct): `New Cairo City`:2851, `Sheikh Zayed City`:1218, `6 October City`:1070, `Hurghada`:503, `Mostakbal City - Future City`:349, `Al Ain Al Sokhna`:242, `Madinaty`:195, `Ras Al Hekma`:192, `New Capital City`:173, `Shorouk City`:162, `Sidi Abdel Rahman`:161, `Hay Sharq`:116, `New Heliopolis`:106, `Al Alamein`:97, `Mokattam`:55, `Qesm Marsa Matrouh`:55, `Qesm Ad Dabaah`:52, `Nasr City`:50, `Hay El Maadi`:49, `Heliopolis - Masr El Gedida`:37, `Alexandria Compounds`:36, `Noor City`:35, `El Nozha`:34, `Obour City`:29, `Dokki`:14
### `payment_methods` & combos
- **Nawy items:** {'installments': 4777}
- **Nawy combos:** {'installments': 4777}
- **PropertyFinder items:** {'cash': 4302, 'installments': 2067}
- **PropertyFinder combos:** {'cash': 3174, 'cash,installments': 1128, '': 2786, 'installments': 939}

## 4. Locations & Cross-Platform Compound Overlap
- **Nawy unique compounds:** 790 (normalized `(city, compound)` pairs: 791)
  - Top 20: C9 (31), Sun Capital (30), La Mirada (30), One33-Phase Two (30), Montenapoleone (29), Hyatt Centric (28), Masaya (27), Noll (27), One33-Phase One (27), The Islands (26), West Clay (26), The Gryd (26), Upmount (26), The Spine (25), Jazebeya (25), City Hall Strip (25), Nyoum Mostaqbal City (24), Nasa mall (24), Moonreal Tower (23), Khan Arabesque (21)
- **PropertyFinder unique compounds:** 1,117 (normalized `(city, compound)` pairs: 1,131)
  - Top 20: Mountain View iCity (150), Palm Hills New Cairo (145), City Gate (144), Mountain View Hyde Park (126), Mivida (101), Mountain View Chill Out Park (99), Badya Palm Hills (96), Garden Residence (94), Taj City (91), District 5 (87), Zed East (86), Privado (85), Sodic East (83), El Patio Oro (80), Mountain View iCity October (76), Vye Sodic (76), Mountain View 1.1 (73), Village West (72), Park Side Residence (64), Marassi (63)
- **Exact normalized `(city, compound)` overlap (Nawy ∩ PropertyFinder):** 15
  - Sample overlaps: [('al alamein', 'marina 1'), ('al alamein', 'marina 2'), ('al alamein', 'marina 4'), ('al alamein', 'marina 5'), ('al alamein', 'marina 6'), ('al alamein', 'marina 7'), ('al alamein', 'porto marina'), ('al alamein', 'stella marina'), ('new capital city', 'boardwalk'), ('new capital city', 'el patio jade'), ('new capital city', 'il mondo'), ('new heliopolis', 'rock white'), ('sidi abdel rahman', 'hacienda red'), ('sidi abdel rahman', 'q north'), ('sidi heneish', 'beit al bahr')]

## 5. Numeric Distributions & Computed `price_per_m2`
### `size_m2`
- **Nawy:** n=4776 | min=5.00 | p5=45.00 | med=147.00 | p95=424.00 | p99=827.75 | max=3,000.00
- **PropertyFinder:** n=8027 | min=20.00 | p5=67.00 | med=180.00 | p95=470.70 | p99=937.00 | max=48,300.00
### `plot_size_m2`
- **Nawy:** n=1060 | min=0.00 | p5=0.00 | med=365.00 | p95=1,105.75 | p99=2,233.48 | max=10,300.00
- **PropertyFinder:** n=1458 | min=120.00 | p5=140.00 | med=270.00 | p95=690.75 | p99=1,100.00 | max=2,200.00
### `bedrooms`
- **Nawy:** n=4756 | min=0.00 | p5=0.00 | med=3.00 | p95=5.00 | p99=7.00 | max=15.00
- **PropertyFinder:** n=8020 | min=0.00 | p5=1.00 | med=3.00 | p95=5.00 | p99=7.00 | max=30.00
### `bathrooms`
- **Nawy:** n=4756 | min=0.00 | p5=0.00 | med=2.00 | p95=5.00 | p99=7.00 | max=15.00
- **PropertyFinder:** n=8022 | min=0.00 | p5=1.00 | med=3.00 | p95=5.00 | p99=7.00 | max=20.00
### `delivery_year`
- **Nawy:** n=4776 | min=2,022.00 | p5=2,025.00 | med=2,028.00 | p95=2,030.00 | p99=2,031.00 | max=2,032.00
- **PropertyFinder:** n=0
### `price_egp`
- **Nawy:** n=4777 | min=340,025.00 | p5=4,042,182.00 | med=13,462,000.00 | p95=62,896,524.40 | p99=142,771,960.00 | max=666,478,750.00
- **PropertyFinder:** n=8027 | min=800,000.00 | p5=3,200,000.00 | med=11,000,000.00 | p95=55,000,000.00 | p99=129,480,000.00 | max=800,000,000.00
### `down_payment_egp`
- **Nawy:** n=3874 | min=22,191.00 | p5=202,259.85 | med=1,221,358.50 | p95=11,260,399.85 | p99=31,566,091.72 | max=80,499,993.00
- **PropertyFinder:** n=1782 | min=1.00 | p5=50.00 | med=2,492,242.00 | p95=19,975,000.00 | p99=35,389,735.98 | max=136,000,000.00
### `installment_years`
- **Nawy:** n=3286 | min=1.00 | p5=5.00 | med=8.00 | p95=12.00 | p99=14.00 | max=15.00
- **PropertyFinder:** n=0
### `price_per_m2`
- **Nawy:** n=0
- **PropertyFinder:** n=8027 | min=8,888.00 | p5=25,083.60 | med=64,569.00 | p95=165,323.70 | p99=249,623.52 | max=3,131,923.00
### Computed `price_egp / size_m2`
- **Nawy:** n=4776 | min=4,290.91 | p5=38,737.50 | med=96,874.09 | p95=280,238.64 | p99=480,578.34 | max=1,833,234.33 | >5% mismatch vs scraped `price_per_m2`: 0
- **PropertyFinder:** n=8027 | min=8,888.89 | p5=25,083.95 | med=64,569.04 | p95=165,323.58 | p99=249,623.41 | max=3,131,923.08 | >5% mismatch vs scraped `price_per_m2`: 0

## 6. Anomalies & Traps Detected
- `Nawy:plot_size<size`: **305**
- `Nawy:calc_ppm2>500k`: **35**
- `PropertyFinder:price>300M`: **13**
- `PropertyFinder:calc_ppm2>500k`: **10**
- `Nawy:size_extreme(<20_or_>5000)`: **9**
- `Nawy:calc_ppm2<5k`: **8**
- `Nawy:price>300M`: **6**
- `PropertyFinder:bedrooms_weird`: **3**
- `PropertyFinder:size_extreme(<20_or_>5000)`: **1**

## 7. Geo & Media Coverage
- `Nawy:lat_lon_both_null`: 69
- `Nawy:lat_lon_ok`: 4,708
- `PropertyFinder:lat_lon_both_null`: 2,786
- `PropertyFinder:lat_lon_ok`: 5,241
- `compound_geometry` types: {'Polygon': 4634}
- **Nawy `image_urls` len:** n=4777 | min=1.00 | p5=4.00 | med=6.00 | p95=6.00 | p99=6.00 | max=6.00
- **PropertyFinder `image_urls` len:** n=8027 | min=0.00 | p5=0.00 | med=3.00 | p95=3.00 | p99=3.00 | max=3.00
- `Nawy:has_floor_plan`: 3,456
- `Nawy:has_master_plan`: 3,743
- `PropertyFinder:0_images`: 2,786
- Top image domains: {'s3.eu-central-1.amazonaws.com': 27155, 'static.shared.propertyfinder.eg': 15718}
- Image extensions: {'.jpg': 25297, '.png': 13337, '.jpeg': 3700, '.webp': 439, '.jfif': 71, '.gif': 14, '.bmp': 12, '.pdf': 2, '(none)': 1}
- `listed_date` samples: ['PropertyFinder: 2026-11-13T15:10:08Z', 'PropertyFinder: 2026-11-13T15:06:27Z', 'PropertyFinder: 2026-11-13T11:49:08Z', 'PropertyFinder: 2026-11-13T11:10:20Z', 'PropertyFinder: 2026-11-13T10:55:13Z']

## 8. Features / Amenities
- Strings with leading/trailing whitespace: **3,611**
- **Nawy feature count/listing:** n=4777 | min=0.00 | p5=0.00 | med=3.00 | p95=9.00 | p99=13.00 | max=17.00 | distinct strings: 51
  - Top 25: Commercial strip (2092), Clubhouse (1519), Outdoor pools (1233), Underground parking (1074), Children's Play Area (1065), Jogging trail (1024), Garden (997), Shared Gym (940), Nanny Room (923), Business Hub (863), bicycles lanes (640), Medical center (633), Has roof (437), Mosque (392), Garage (348), Barbecue Area (285), Sports Clubs (275), Shared Spa (270), Terrace (230), Driver Room (209), Infrastructure (205), Concierge (204), Food Court (191), Charging Electric Cars Units (187), Disability support (159)
- **PropertyFinder feature count/listing:** n=8027 | min=0.00 | p5=0.00 | med=9.00 | p95=23.00 | p99=23.00 | max=23.00 | distinct strings: 23
  - Top 25: Covered Parking (5199), Shared Gym (5199), Shared Spa (5005), Shared Pool (4765), View of Landmark (4667), View of Water (4208), Lobby in Building (4157), Private Garden (4009), Clubhouse (3950), Children's Pool (3896), Study (3889), Central A/C (3634), Maids Room (3580), Elevator (3483), Sports Courts (3429), Kitchen Appliances (3339), Medical Center (3183), Service Elevator (2993), Storage Room (2861), Laundry Room (2777), Double Glazed Windows (2768), Private Pool (2577), Solar Panels (2425)

## 9. Text, Arabic Hygiene & LLM Extraction Need
- **Nawy description char length:** n=4777 | min=0.00 | p5=141.00 | med=186.00 | p95=262.00 | p99=299.24 | max=432.00
- **PropertyFinder description char length:** n=8027 | min=0.00 | p5=0.00 | med=546.00 | p95=1,748.00 | p99=1,975.00 | max=2,205.00
- `PropertyFinder:arabic_in_desc`: 496
- `PropertyFinder:arabic_in_location`: 1
- `PropertyFinder:arabic_in_title`: 489
- `PropertyFinder:arabic_indic_digits`: 42
- `PropertyFinder:contact_hidden_placeholder`: 771
- `PropertyFinder:hashtags_in_text`: 133
- `PropertyFinder:phone_numbers_in_text`: 7
- `PropertyFinder:tashkeel_or_tatweel`: 189
### Missing Fields That Have Text Cues (Regex vs Groq Budget Estimate)
- **Nawy:** {'payment_terms_incomplete': 1525, 'payment_incomplete_but_cue_in_text': 531, 'delivery_year_is_null': 1}
- **PropertyFinder:** {'finishing_is_null': 7831, 'finishing_null_but_cue_in_text': 3860, 'payment_terms_incomplete': 8027, 'payment_incomplete_but_cue_in_text': 2640, 'delivery_year_is_null': 8027, 'delivery_null_but_cue_in_text': 3089}

## 10. Rough Deduplication Bucket Estimate
- Total rows: 12,804
- Distinct coarse buckets `(compound_or_city, type, beds, size_5m_bin)`: **10,029**
- Buckets hit by >1 platform: **105**

## 11. Concrete Sample Anomalies (Truncated)
### Bucket: `high_ppm2`
```json
{"reason": "calc_ppm2=504902", "platform": "Nawy", "source_id": 132604, "title_raw": "Medical for sale in Safa Medi Plex Phase 2 in New Cairo by Hyde Park", "property_type": "Medical", "size_m2": 51, "price_egp": 25750000, "currency": "EGP", "down_payment_egp": 2575000, "price_per_m2": null, "finishing": "Not Finished Core Shell", "governorate": null, "city": "New Cairo", "compound": "Safa Medi Plex Phase 2", "lat_lon": [29.99461983333333, 31.54650116666667], "desc_snippet": "A Medical in Safa Medi Plex Phase 2 by Hyde Park. The Medical size is 51 m².\n\nThis property will be ready for delivery not finished by 2027-08-30."}
```
```json
{"reason": "calc_ppm2=510443", "platform": "Nawy", "source_id": 133603, "title_raw": "Apartment for sale in Nobu Residence with 1 bedroom in Ras El Hekma by SODIC", "property_type": "Apartment", "size_m2": 97, "price_egp": 49513000, "currency": "EGP", "down_payment_egp": 2475650, "price_per_m2": null, "finishing": "Fully Finished Furnished", "governorate": null, "city": "Ras El Hekma", "compound": "Nobu Residence", "lat_lon": [31.087269919999997, 27.990749], "desc_snippet": "A 1 bedroom Apartment in Nobu Residence by SODIC. The Apartment size is 97 m² with 2 bathrooms\nwith a garden of 29 m²\n.\n\nThis property will be ready for delivery furnished by 2030-08-30."}
```
```json
{"reason": "calc_ppm2=613190", "platform": "Nawy", "source_id": 136083, "title_raw": "Studio for sale in Nobu Residence in Ras El Hekma by SODIC", "property_type": "Studio", "size_m2": 63, "price_egp": 38631000, "currency": "EGP", "down_payment_egp": 1931550, "price_per_m2": null, "finishing": "Fully Finished Furnished", "governorate": null, "city": "Ras El Hekma", "compound": "Nobu Residence", "lat_lon": [31.087269919999997, 27.990749], "desc_snippet": "A Studio in Nobu Residence by SODIC. The Studio size is 63 m² with 1 bathrooms\nwith a garden of 30 m²\n.\n\nThis property will be ready for delivery furnished by 2030-08-30."}
```
### Bucket: `price_outlier_high`
```json
{"reason": "price=666478750.0", "platform": "Nawy", "source_id": 138908, "title_raw": "Retail for sale in The Spine", "property_type": "Retail", "size_m2": 1361, "price_egp": 666478750, "currency": "EGP", "down_payment_egp": 9997181, "price_per_m2": null, "finishing": "Not Finished Core Shell", "governorate": null, "city": "New Cairo", "compound": "The Spine", "lat_lon": [30.100148647058827, 31.66339041176471], "desc_snippet": "A Retail in The Spine by Talaat Moustafa Group (TMG) Holding. The Retail size is 1361 m².\n\nThis property will be ready for delivery not finished by 2031-11-01."}
```
```json
{"reason": "price=649569200.0", "platform": "Nawy", "source_id": 138910, "title_raw": "Retail for sale in The Spine", "property_type": "Retail", "size_m2": 1215, "price_egp": 649569200, "currency": "EGP", "down_payment_egp": 9743538, "price_per_m2": null, "finishing": "Not Finished Core Shell", "governorate": null, "city": "New Cairo", "compound": "The Spine", "lat_lon": [30.100148647058827, 31.66339041176471], "desc_snippet": "A Retail in The Spine by Talaat Moustafa Group (TMG) Holding. The Retail size is 1215 m².\n\nThis property will be ready for delivery not finished by 2031-11-01."}
```
```json
{"reason": "price=569770150.0", "platform": "Nawy", "source_id": 138909, "title_raw": "Retail for sale in The Spine", "property_type": "Retail", "size_m2": 1151, "price_egp": 569770150, "currency": "EGP", "down_payment_egp": 8546552, "price_per_m2": null, "finishing": "Not Finished Core Shell", "governorate": null, "city": "New Cairo", "compound": "The Spine", "lat_lon": [30.100148647058827, 31.66339041176471], "desc_snippet": "A Retail in The Spine by Talaat Moustafa Group (TMG) Holding. The Retail size is 1151 m².\n\nThis property will be ready for delivery not finished by 2031-11-01."}
```
### Bucket: `size_outlier`
```json
{"reason": "size=16.0", "platform": "Nawy", "source_id": 106200, "title_raw": "Retail for resale in Catalan Mall in New Capital City by Gates Development", "property_type": "Retail", "size_m2": 16, "price_egp": 4500000, "currency": "EGP", "down_payment_egp": 3200000, "price_per_m2": null, "finishing": "Not Finished Core Shell", "governorate": null, "city": "New Capital City", "compound": "Catalan Mall", "lat_lon": [29.954320818181817, 31.724968818181818], "desc_snippet": "Retail For Sale in Catalan Mall, New Capital City\n\nFloor: Ground\n\nBuilt up area: 16\n\nFinishing: Core & Shell\n\nDelivery: Off plan\n\nPrice: 4,500,000\n\nDown payment: 3,200,000\n\nRemaining: 1,300,000\n\nMaintenance: 186,781"}
```
```json
{"reason": "size=13.0", "platform": "Nawy", "source_id": 138369, "title_raw": "Medical for sale in Nasa mall in 6th of October City by Kayan Real Estate", "property_type": "Medical", "size_m2": 13, "price_egp": 10206900, "currency": "EGP", "down_payment_egp": 255173, "price_per_m2": null, "finishing": "Fully Finished", "governorate": null, "city": "6th of October City", "compound": "Nasa mall", "lat_lon": [null, null], "desc_snippet": "A Medical in Nasa mall by Kayan Real Estate. The Medical size is 13 m².\n\nThis property will be ready for delivery finished by 2030-10-30."}
```
```json
{"reason": "size=17.0", "platform": "Nawy", "source_id": 59225, "title_raw": "Office for resale in Vida Mall Down Town  in New Capital City by Location Developments", "property_type": "Office", "size_m2": 17, "price_egp": 1450000, "currency": "EGP", "down_payment_egp": 1000000, "price_per_m2": null, "finishing": "Fully Finished", "governorate": null, "city": "New Capital City", "compound": "Vida Mall Down Town ", "lat_lon": [30.003162500000002, 31.734967833333332], "desc_snippet": "Office For Sale In Vida Mall Down Town New Capital"}
```
### Bucket: `low_ppm2_suspect_dp`
```json
{"reason": "calc_ppm2=4544", "platform": "Nawy", "source_id": 126165, "title_raw": "Apartment for sale in Shedwan with 2 bedrooms in El Gouna by Orascom Development Egypt", "property_type": "Apartment", "size_m2": 109, "price_egp": 495320, "currency": "USD", "down_payment_egp": 74298, "price_per_m2": null, "finishing": "Fully Finished", "governorate": null, "city": "El Gouna", "compound": "Shedwan", "lat_lon": [27.400481027285522, 33.65622896483563], "desc_snippet": "A 2 bedroom Apartment in Shedwan by Orascom Development Egypt. The Apartment size is 110 m² with 3 bathrooms.\n\nThis property will be ready for delivery finished by 2028-06-29."}
```
```json
{"reason": "calc_ppm2=4908", "platform": "Nawy", "source_id": 126163, "title_raw": "Apartment for sale in Shedwan with 1 bedroom in El Gouna by Orascom Development Egypt", "property_type": "Apartment", "size_m2": 85, "price_egp": 417165, "currency": "USD", "down_payment_egp": 62575, "price_per_m2": null, "finishing": "Fully Finished", "governorate": null, "city": "El Gouna", "compound": "Shedwan", "lat_lon": [27.400481027285522, 33.65622896483563], "desc_snippet": "A 1 bedroom Apartment in Shedwan by Orascom Development Egypt. The Apartment size is 89 m² with 2 bathrooms\nwith a garden of 200 m²\n.\n\nThis property will be ready for delivery finished by 2028-06-29."}
```
```json
{"reason": "calc_ppm2=4304", "platform": "Nawy", "source_id": 126164, "title_raw": "Apartment for sale in Shedwan with 1 bedroom in El Gouna by Orascom Development Egypt", "property_type": "Apartment", "size_m2": 79, "price_egp": 340025, "currency": "USD", "down_payment_egp": 54353, "price_per_m2": null, "finishing": "Fully Finished", "governorate": null, "city": "El Gouna", "compound": "Shedwan", "lat_lon": [27.400481027285522, 33.65622896483563], "desc_snippet": "A 1 bedroom Apartment in Shedwan by Orascom Development Egypt. The Apartment size is 79 m² with 2 bathrooms.\n\nThis property will be ready for delivery finished by 2028-06-29."}
```
