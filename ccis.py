"""Drive CCIS with Selenium: sign in, search one person, capture a PDF.

Ported from Project-Rap-Sheet's ccis.py, which is itself venire_3.0's proven
browser sequence. The clicks and waits are unchanged, element for element.
What changes for HRS: settings come from the validated config with no
defaults, and no name, DOB or CCIS address goes into a log line or an error
message. The caller logs the spreadsheet row instead.

Carried over and worth knowing: _search() clicks the "exclude attorneys" and
"exclude judges" checkboxes on every search and relies on _reset_search()
having cleared them. If a reset ever stops clearing them, the second click
turns the exclusions back off, and the search quietly widens to attorneys
and judges who share the person's name. venire and Rap-Sheet run clean with
this, so it is kept as-is.
"""

import base64
import time
from typing import NamedTuple

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from exceptions import RowProblem, SystemProblem
from logger import get_logger

logger = get_logger(__name__)


class CcisResult(NamedTuple):
    """What one search produced: the PDF, and whether CCIS had any record.

    has_record is False when the PDF is the printed "No matches found." page.
    """

    pdf_bytes: bytes
    has_record: bool


# Chrome DevTools Protocol, for a PDF with no print dialog.
PRINT_TO_PDF_COMMAND = "Page.printToPDF"

# Element locators, from venire_3.0's config.json by way of Rap-Sheet. The
# ccis.locators section of config.yaml overrides any of them, so a CCIS
# release that renames a field is a config edit rather than a code change.
DEFAULT_LOCATORS = {
    "username_field": "loginForm:username",
    "password_field": "loginForm:password",
    "submit_login_button": "loginForm:login",
    "exclude_attorneys_checkbox": "//label[@for='search_tab:personForm:nameTypes:0']",
    "exclude_judges_checkbox": "//label[@for='search_tab:personForm:nameTypes:1']",
    "last_name_field": "search_tab:personForm:lastname",
    "first_name_field": "search_tab:personForm:fname",
    "dob_field": "search_tab:personForm:dob_input",
    "search_button": "//button[.//span[text()='Search']]",
    "no_results_popup_message": (
        "//span[@class='ui-messages-info-detail' and text()='No matches found.']"
    ),
    "view_selection_button": "//button[.//span[text()='view selection']]",
    "back_button_from_pdf_page": "//button[contains(@id, 'caseSummary')]",
    "back_button_from_selection_page": (
        "//button[contains(@onclick, 'PrimeFaces.ab') and "
        "not(contains(@onclick, 'caseSummary'))]"
    ),
    "reset_button": "//button[.//span[text()='Reset']]",
}

# The window size is not cosmetic: the PDF is rendered from the page, so a
# narrow viewport changes what the captured case summary looks like.
HEADLESS_ARGUMENTS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1920,1080",
)


class CcisSession:
    """One signed-in CCIS session, reused for every person in a run.

    Used as `with CcisSession(config) as ccis:` so Chrome closes even when a
    search blows up partway through.
    """

    def __init__(self, config: dict) -> None:
        """Take the CCIS settings and login from a config built by load_config."""
        ccis = config["ccis"]
        self.url: str = ccis["url"]
        self._username: str = ccis["username"]
        self._password: str = ccis["password"]
        self.headless: bool = ccis["headless"]
        self.wait_timeout: float = ccis["wait_timeout"]
        self.no_results_wait_timeout: float = ccis["no_results_wait_timeout"]
        self.pause_between_actions: float = ccis["pause_between_actions"]
        self.locators = {**DEFAULT_LOCATORS, **ccis["locators"]}
        self._chromedriver = config["paths"]["chromedriver"]

        self.driver: webdriver.Chrome | None = None
        self.wait: WebDriverWait | None = None

    def __enter__(self) -> "CcisSession":
        """Open and sign in on entering the with block."""
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        """Close Chrome on leaving the with block, however it is left."""
        self.close()

    def _at(self, name: str) -> str:
        """Return the locator for a named element."""
        return self.locators[name]

    def open(self) -> None:
        """Launch Chrome, load CCIS and sign in.

        Raises SystemProblem when Chrome will not start, CCIS will not load,
        or the login is refused. None of those belong to any one person.
        """
        logger.info("Opening CCIS (headless: %s)", self.headless)
        self.driver = self._start_chrome()
        self.wait = WebDriverWait(self.driver, self.wait_timeout)

        try:
            self.driver.get(self.url)
            if not self.headless:
                self.driver.maximize_window()
        except WebDriverException as error:
            self.close()
            raise SystemProblem(f"Could not load CCIS: {type(error).__name__}") from error

        try:
            self._sign_in()
        except Exception:
            self.close()
            raise

    def _start_chrome(self) -> webdriver.Chrome:
        """Start Chrome, trying each way of finding chromedriver in turn.

        The downloader goes last because on a locked-down network it is the
        route least likely to work.
        """
        options = webdriver.ChromeOptions()
        if self.headless:
            for argument in HEADLESS_ARGUMENTS:
                options.add_argument(argument)

        attempts = []
        if self._chromedriver:
            attempts.append(("paths.chromedriver", lambda: Service(str(self._chromedriver))))
        attempts.append(("Selenium's own driver lookup", lambda: None))
        attempts.append(
            ("downloading chromedriver", lambda: Service(ChromeDriverManager().install()))
        )

        failures = []
        for description, build_service in attempts:
            try:
                service = build_service()
                driver = (
                    webdriver.Chrome(service=service, options=options)
                    if service
                    else webdriver.Chrome(options=options)
                )
                logger.debug("Chrome started via %s", description)
                return driver
            except Exception as error:  # noqa: BLE001 - each route fails differently
                failures.append(
                    f"{description}: {type(error).__name__}: {str(error).splitlines()[0]}"
                )

        raise SystemProblem(
            "Could not start Chrome. Set paths.chromedriver in config.yaml to a "
            "chromedriver.exe matching your Chrome version. Tried: "
            + " | ".join(failures)
        )

    def _sign_in(self) -> None:
        """Fill the sign-in form and wait for the search page.

        The last-name field only exists on the search page, so its arrival
        is the proof that sign-in worked.
        """
        try:
            self.wait.until(
                EC.element_to_be_clickable((By.ID, self._at("username_field")))
            ).send_keys(self._username)
            self.wait.until(
                EC.element_to_be_clickable((By.ID, self._at("password_field")))
            ).send_keys(self._password)
            self.driver.find_element(By.ID, self._at("submit_login_button")).click()
        except (TimeoutException, WebDriverException) as error:
            raise SystemProblem(
                f"Could not fill in the CCIS sign-in form: {type(error).__name__}"
            ) from error

        try:
            self.wait.until(
                EC.presence_of_element_located((By.ID, self._at("last_name_field")))
            )
        except TimeoutException as error:
            raise SystemProblem(
                "Signed in to CCIS but the search page never appeared. Usually a "
                "wrong username or password in Windows Credential Manager."
            ) from error

        logger.info("Signed in to CCIS")

    def close(self) -> None:
        """Close Chrome. Safe to call twice, and never raises."""
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except WebDriverException as error:
            logger.warning("Chrome did not close cleanly: %s", type(error).__name__)
        finally:
            self.driver = None
            self.wait = None

    def fetch_pdf(self, first_name: str, last_name: str, dob: str) -> CcisResult:
        """Search for one person and return a PDF of what CCIS showed.

        dob is mm/dd/yyyy. With a record, the PDF is the case summary. With
        none, it is the printed "No matches found." page, taken while the
        banner and the search criteria are still on screen so it shows who
        was searched. No record is an ordinary answer, not a failure.

        The search form is always reset afterward, in a finally, so a failure
        cannot leave a half-filled form for the next person.

        Raises RowProblem when CCIS did not behave as expected for this
        person, and SystemProblem when the browser itself is broken.
        """
        try:
            self._search(first_name, last_name, dob)

            if self._no_results():
                return CcisResult(self._print_to_pdf(), has_record=False)

            self._open_case_summary()
            pdf_bytes = self._print_to_pdf()
            self._back_to_search()
            return CcisResult(pdf_bytes, has_record=True)

        except (RowProblem, SystemProblem):
            raise
        except TimeoutException as error:
            raise RowProblem("CCIS did not respond as expected (timed out)") from error
        except WebDriverException as error:
            # A dead driver is not this person's fault, and the next search
            # would fail the same way, so it stops the run.
            raise SystemProblem(f"The browser failed: {type(error).__name__}") from error
        finally:
            self._reset_search()

    def _search(self, first_name: str, last_name: str, dob: str) -> None:
        """Fill the search form and submit it.

        The two checkbox clicks exclude attorneys and judges, who otherwise
        turn up alongside people of the same name.
        """
        self.driver.find_element(By.XPATH, self._at("exclude_attorneys_checkbox")).click()
        self.driver.find_element(By.XPATH, self._at("exclude_judges_checkbox")).click()

        self.wait.until(
            EC.element_to_be_clickable((By.ID, self._at("last_name_field")))
        ).send_keys(last_name)
        self.wait.until(
            EC.element_to_be_clickable((By.ID, self._at("first_name_field")))
        ).send_keys(first_name)
        self.wait.until(
            EC.element_to_be_clickable((By.ID, self._at("dob_field")))
        ).send_keys(dob)
        self.wait.until(
            EC.element_to_be_clickable((By.XPATH, self._at("search_button")))
        ).click()

    def _no_results(self) -> bool:
        """True when CCIS put up its "No matches found." banner.

        Uses its own short wait, because most people with a record never
        show the banner and the full timeout would be added to each of them.
        Only TimeoutException is caught, so a dead driver still propagates.
        """
        brief = WebDriverWait(self.driver, self.no_results_wait_timeout)
        try:
            brief.until(
                EC.visibility_of_element_located(
                    (By.XPATH, self._at("no_results_popup_message"))
                )
            )
            return True
        except TimeoutException:
            return False

    def _open_case_summary(self) -> None:
        """Click through to the case summary page the PDF is taken from."""
        self.wait.until(
            EC.element_to_be_clickable((By.XPATH, self._at("view_selection_button")))
        ).click()
        # The back button only exists on the summary page, so waiting for it
        # proves the page has arrived before printing.
        self.wait.until(
            EC.element_to_be_clickable((By.XPATH, self._at("back_button_from_pdf_page")))
        )

    def _print_to_pdf(self) -> bytes:
        """Capture the current page as PDF bytes through Chrome DevTools.

        Background graphics are kept because the case summary uses shading to
        separate its sections, and it is hard to read without them.
        """
        try:
            result = self.driver.execute_cdp_cmd(
                PRINT_TO_PDF_COMMAND, {"printBackground": True}
            )
        except WebDriverException as error:
            raise SystemProblem(
                f"Chrome would not print the CCIS page to PDF: {type(error).__name__}"
            ) from error

        encoded = result.get("data")
        if not encoded:
            raise RowProblem("Chrome returned an empty PDF")

        pdf_bytes = base64.b64decode(encoded)
        if not pdf_bytes.startswith(b"%PDF-"):
            raise RowProblem("What Chrome returned is not a PDF")
        return pdf_bytes

    def _back_to_search(self) -> None:
        """Walk back from the summary page to the search page."""
        self.wait.until(
            EC.element_to_be_clickable((By.XPATH, self._at("back_button_from_pdf_page")))
        ).click()
        self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, self._at("back_button_from_selection_page"))
            )
        ).click()

    def _reset_search(self) -> None:
        """Clear the search form for the next person.

        Never raises: it runs in a finally, and an exception here would hide
        the real error that sent the search down that path. A failed reset
        is logged and left for the next search to trip over.
        """
        try:
            self.wait.until(
                EC.element_to_be_clickable((By.XPATH, self._at("reset_button")))
            ).click()
            if self.pause_between_actions > 0:
                time.sleep(self.pause_between_actions)
        except (TimeoutException, WebDriverException) as error:
            logger.warning("Could not reset the CCIS search form: %s", type(error).__name__)
