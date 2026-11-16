# Piper voice models used by ProntoF

The worker downloads each selected voice on first use and caches its ONNX model and configuration under `PIPER_VOICE_DIR` (normally `/data/piper-voices`). Models are not copied into the order folder and are not deleted with temporary MP4 results.

| Language | Voice ID |
|---|---|
| Arabic | `ar_JO-kareem-medium` |
| Bulgarian | `bg_BG-dimitar-medium` |
| Czech | `cs_CZ-jirka-medium` |
| German | `de_DE-thorsten-medium` |
| English | `en_US-lessac-medium` |
| Spanish | `es_ES-davefx-medium` |
| French | `fr_FR-siwis-medium` |
| Italian | `it_IT-paola-medium` |
| Lithuanian | `lt_LT-ingute-medium` |
| Latvian | `lv_LV-aivars-medium` |
| Norwegian | `no_NO-talesyntese-medium` |
| Polish | `pl_PL-darkman-medium` |
| Portuguese | `pt_BR-faber-medium` |
| Russian | `ru_RU-irina-medium` |
| Slovak | `sk_SK-lili-medium` |
| Ukrainian | `uk_UA-ukrainian_tts-medium` |

## Voice-model licences and attribution

The Piper engine and each voice model have separate licences. Review the upstream `MODEL_CARD` for every model before redistributing or using generated audio commercially.

- Lithuanian `lt_LT-ingute-medium`: [upstream model card](https://huggingface.co/rhasspy/piper-voices/blob/main/lt/lt_LT/ingute/medium/MODEL_CARD). The model uses the NewGenLTU Open RAIL-D License 1.0 and has additional use-based restrictions. The model card requires generated speech to be disclosed as synthetic and gives attribution as *Ingutė (lt_LT-ingute-medium), Robertas & Claude, grown from Reginutė*. Confirm that the intended service use complies with the full licence before production deployment.
- Latvian `lv_LV-aivars-medium`: [upstream model card](https://huggingface.co/rhasspy/piper-voices/blob/main/lv/lv_LV/aivars/medium/MODEL_CARD); the upstream voice card identifies the source dataset as CC0 1.0.
- Other voice models: use the licence and attribution stated in each corresponding upstream model card in [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).

The application labels the result as AI-generated speech in the interface and stores an AI-speech disclosure in MP4 metadata. The label does not replace any model-specific licence obligations.
