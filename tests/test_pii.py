import logging

from core.pii import PIIMaskingFilter, mask_pii


def test_masks_email():
    assert mask_pii("contact jane.doe+x@example.com now") == "contact [EMAIL] now"


def test_masks_card_number_keeping_last_four():
    assert mask_pii("VISA 4111 1111 1111 1234") == "VISA ************1234"
    assert mask_pii("card 4111-1111-1111-1234") == "card ************1234"


def test_masks_long_ids():
    assert mask_pii("GST ID: 000123456789") == "GST ID: ********6789"


def test_masks_phone_numbers():
    assert mask_pii("Tel: 03-1234 5678") == "Tel: [PHONE]"
    assert mask_pii("Mobile +6012-345 6789") == "Mobile [PHONE]"


def test_masks_labelled_names():
    assert mask_pii("Cashier: AMIRAH BINTI ALI") == "Cashier: [NAME]"
    assert mask_pii("Bill To: John Smith\nTotal 9.50") == "Bill To: [NAME]\nTotal 9.50"


def test_keeps_amounts_dates_and_times():
    text = "Date: 14/03/2018 10:32:11  Total 1,234.56  Rp 1.250.000  2018-03-14  14-03-2018"
    assert mask_pii(text) == text


def test_logging_filter_masks_formatted_message(caplog):
    logger = logging.getLogger("test_pii")
    logger.addFilter(PIIMaskingFilter())
    with caplog.at_level(logging.INFO, logger="test_pii"):
        logger.info("user %s paid with %s", "jane@example.com", "4111 1111 1111 1234")
    assert caplog.records[0].getMessage() == "user [EMAIL] paid with ************1234"
