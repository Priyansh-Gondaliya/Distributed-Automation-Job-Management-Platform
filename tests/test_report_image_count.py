from app import database


def test_ignores_image_mentions_without_download():
    log = """
    Running C:\\scripts\\logo.png_helper.py
    Traceback (most recent call last):
      File "C:\\app\\icon.png.py", line 1
    https://cdn.example.com/banner.jpg
    Done.
    """
    counts = database.counts_from_job_log(log)
    assert counts["image_count"] == 0
    assert database.log_has_image_download_evidence(log) is False


def test_counts_explicit_total_images():
    log = "Total images: 12\nSaved output."
    counts = database.counts_from_job_log(log)
    assert counts["image_count"] == 12
    assert database.log_has_image_download_evidence(log) is True


def test_total_images_zero_stays_zero():
    log = "Total images: 0\nhttps://cdn.example.com/a.jpg"
    counts = database.counts_from_job_log(log)
    assert counts["image_count"] == 0


def test_image_downloaded_lines():
    log = "Image downloaded: page1.jpg\nImage downloaded: page2.jpg"
    counts = database.counts_from_job_log(log)
    assert counts["image_count"] == 2


def test_file_downloaded_pdf_is_not_image():
    log = "File downloaded: edition.pdf\nSaved 1 pdfs"
    counts = database.counts_from_job_log(log)
    assert counts["image_count"] == 0
    assert counts["pdf_count"] == 1
