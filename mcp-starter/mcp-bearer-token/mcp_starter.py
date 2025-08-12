import asyncio
from typing import Annotated
import os
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent, ImageContent, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, AnyUrl
import os
import random
import string
from typing import Literal, Optional
from textwrap import dedent  # added

import markdownify
import httpx
import readabilipy
import os
from supabase import create_client, Client

# Get your Supabase credentials from environment variables
# You would set these in your deployment environment
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Create the Supabase client
supabase: Client = create_client("https://wqirjtgjnvqgkmwjkuof.supabase.co", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndxaXJqdGdqbnZxZ2ttd2prdW9mIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTQ4NDI4NTAsImV4cCI6MjA3MDQxODg1MH0.nsfI5G15rbrceJN5pczDWpLHU4H9EKFtKhdWEgFiPxE")
# supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Load environment variables ---
load_dotenv()

TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")

assert TOKEN is not None, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER is not None, "Please set MY_NUMBER in your .env file"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="puch-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- Rich Tool Description model ---
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: str | None = None

# --- Fetch Utility Class ---
class Fetch:
    USER_AGENT = "Puch/1.0 (Autonomous)"

    @classmethod
    async def fetch_url(
        cls,
        url: str,
        user_agent: str,
        force_raw: bool = False,
    ) -> tuple[str, str]:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    url,
                    follow_redirects=True,
                    headers={"User-Agent": user_agent},
                    timeout=30,
                )
            except httpx.HTTPError as e:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url}: {e!r}"))

            if response.status_code >= 400:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url} - status code {response.status_code}"))

            page_raw = response.text

        content_type = response.headers.get("content-type", "")
        is_page_html = "text/html" in content_type

        if is_page_html and not force_raw:
            return cls.extract_content_from_html(page_raw), ""

        return (
            page_raw,
            f"Content type {content_type} cannot be simplified to markdown, but here is the raw content:\n",
        )

    @staticmethod
    def extract_content_from_html(html: str) -> str:
        """Extract and convert HTML content to Markdown format."""
        ret = readabilipy.simple_json.simple_json_from_html_string(html, use_readability=True)
        if not ret or not ret.get("content"):
            return "<error>Page failed to be simplified from HTML</error>"
        content = markdownify.markdownify(ret["content"], heading_style=markdownify.ATX)
        return content

    @staticmethod
    async def google_search_links(query: str, num_results: int = 5) -> list[str]:
        """
        Perform a scoped DuckDuckGo search and return a list of job posting URLs.
        (Using DuckDuckGo because Google blocks most programmatic scraping.)
        """
        ddg_url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        links = []

        async with httpx.AsyncClient() as client:
            resp = await client.get(ddg_url, headers={"User-Agent": Fetch.USER_AGENT})
            if resp.status_code != 200:
                return ["<error>Failed to perform search.</error>"]

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", class_="result__a", href=True):
            href = a["href"]
            if "http" in href:
                links.append(href)
            if len(links) >= num_results:
                break

        return links or ["<error>No results found.</error>"]

# --- MCP Server Setup ---
mcp = FastMCP(
    "Assignment Manager MCP Server",  # renamed from "Job Finder MCP Server"
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool Definition ---
AssignmentManagerDescription = RichToolDescription(
    description="Manages assignments for teachers and students. Can create assignments, accept submissions, and list submissions.",
    use_when="Use this for any requests related to creating, submitting, or viewing school assignments.",
    side_effects="Interacts with a database to store and retrieve assignment information.",
)

# Helper function to generate a unique ID
def generate_unique_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

AssignmentManagerDescription = RichToolDescription(
    description="A friendly WhatsApp assistant for managing school assignments. It helps teachers create submission 'dropboxes' and view submitted work. It guides students step-by-step through the process of submitting their assignments using a unique ID and a Google Drive link.",
    use_when="Use this for any user message about creating, submitting, viewing, or asking for help with school assignments. This tool manages the entire workflow for both teachers and students.",
    side_effects="Interacts with a Supabase database to store and retrieve assignment and submission data. It can create new records or query existing ones.",
)

@mcp.tool(description=AssignmentManagerDescription.model_dump_json())
async def assignment_manager(
    user_phone: Annotated[str, Field(description="The user's phone number, automatically provided by the system.")],
    # --- The intent is now Optional to handle greetings ---
    user_intent: Annotated[Optional[Literal["create", "submit", "view"]], Field(description="The primary action the user wants to perform.")] = None,
    submission_id: Annotated[Optional[str], Field(description="The 6-digit ID for an assignment. Required for 'submit' and 'view' intents.")] = None,
    drive_link: Annotated[Optional[str], Field(description="The Google Drive URL for a student's submission. Required for the 'submit' intent.")] = None,
) -> str:
    """
    Handles assignment creation for teachers, and submissions & viewing for students/teachers with improved, conversational prompts.
    """

    # --- 💡 NEW: Handle greetings and general help requests ---
    if not user_intent:
        return (
            "Hello! I'm your friendly **AssignmentDrop assistant** 🤖.\n\n"
            "I can help you manage assignments right here in WhatsApp.\n\n"
            "🔹 **For Teachers:** You can say *'Create an assignment'* to get started.\n"
            "🔹 **For Students:** You can say *'I want to submit my work'* to begin a submission.\n\n"
            "How can I help you today?"
        )

    # --- Intent 1: Teacher creates an assignment ---
    if user_intent == "create":
        new_id = generate_unique_id()
        try:
            supabase.table("assignments").insert({
                "submission_id": new_id,
                "teacher_phone": user_phone
            }).execute()
            # --- 💡 IMPROVED Prompt ---
            return (
                f"✅ All set! Your new assignment dropbox has been created.\n\n"
                f"The unique submission ID is: *{new_id}*\n\n"
                f"Please share this code with your students. They will need it to submit their work."
            )
        except Exception as e:
            print(f"Error creating assignment: {e}")
            raise McpError(ErrorData(code="DB_ERROR", message="I'm sorry, I encountered a database error and couldn't create the assignment. Please try again later."))

    # --- Intent 2: Student submits an assignment (with guided steps) ---
    if user_intent == "submit":
        # --- 💡 IMPROVED Guided Flow ---
        # Step 1: Ask for the submission ID if it's missing
        if not submission_id:
            return "Of course! To submit your assignment, I first need the **6-digit assignment ID** your teacher gave you. What is the ID?"

        # Step 2: Check if the ID exists before asking for the link
        try:
            check_response = supabase.table("assignments").select("id").eq("submission_id", submission_id).execute()
            if not check_response.data:
                return f"❌ It seems the assignment ID '{submission_id}' is not valid. Please double-check the code with your teacher and try again."
        except Exception as e:
            print(f"Error checking submission ID: {e}")
            raise McpError(ErrorData(code="DB_ERROR", message="I'm having trouble verifying the assignment ID right now. Please try again in a moment."))

        # Step 3: Ask for the Drive link if the ID is valid but the link is missing
        if not drive_link:
            return "Great, I've found that assignment! Now, please reply with the **shareable Google Drive link** to your file. (Remember to set the link's permission to 'Anyone with the link can view')."

        # Step 4: If both ID and link are present, process the submission
        try:
            supabase.table("submissions").insert({
                "assignment_submission_id": submission_id,
                "student_phone": user_phone,
                "drive_link": drive_link
            }).execute()
            return "✅ Thank you! Your submission has been successfully received. Well done!"
        except Exception as e:
            print(f"Error submitting assignment: {e}")
            raise McpError(ErrorData(code="DB_ERROR", message="I'm sorry, there was a problem saving your submission. Please try sending the link again."))

    # --- Intent 3: Teacher views submissions ---
    if user_intent == "view":
        if not submission_id:
            return "Sure, I can show you the submissions. Which assignment are you interested in? Please provide the **6-digit assignment ID**."

        try:
            response = supabase.table("submissions").select("student_phone, drive_link, submitted_at").eq("assignment_submission_id", submission_id).order("submitted_at", desc=True).execute()

            # --- 💡 IMPROVED No-Submissions Prompt ---
            if not response.data:
                return f"It looks like there are no submissions for assignment ID *{submission_id}* just yet. Once students start submitting their work, you'll see them listed here."

            # --- 💡 IMPROVED List Formatting ---
            submission_list = [
                f"👤 From: *{item['student_phone']}*\n🔗 Link: {item['drive_link']}\n🗓️ Date: {item['submitted_at'].split('T')[0]}"
                for item in response.data
            ]
            formatted_response = f"📚 Here are the submissions for Assignment ID *{submission_id}*:\n\n" + "\n---\n".join(submission_list)
            return formatted_response

        except Exception as e:
            print(f"Error viewing submissions: {e}")
            raise McpError(ErrorData(code="DB_ERROR", message="Sorry, I couldn't fetch the submissions due to a database error. Please try again."))

    # Fallback error if an invalid intent is somehow passed
    raise McpError(ErrorData(code="INVALID_INTENT", message="I'm not sure how to help with that. You can ask me to 'create', 'submit', or 'view' an assignment."))







# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    return MY_NUMBER

@mcp.tool
async def about() -> dict[str, str]:
    server_name = "Assignment Manager MCP Server"
    server_description = dedent("""
    This MCP server powers a WhatsApp assistant that helps teachers and students manage school/college assignments.

    Provided tools:
    - assignment_manager: Create assignment dropboxes (teachers), accept student submissions (by submission ID + Drive link), and view submissions.
    - validate: Returns the configured service number used by Puch.

    Key integrations and behavior:
    - Authentication via a simple Bearer token provider.
    - Data persistence with Supabase (tables: assignments, submissions).
    - Helpful, conversational flows for greeting, creating, submitting, and viewing.
    - Runs over streamable-http on 0.0.0.0:8086.
    """).strip()

    return {
        "name": server_name,
        "description": server_description
    }

# --- Run MCP Server ---
async def main():
    print("🚀 Starting MCP server on http://0.0.0.0:8086")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())
