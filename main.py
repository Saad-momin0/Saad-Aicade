import streamlit as st
import subprocess
from pydantic import BaseModel, Field

import anthropic
import instructor

from pathlib import Path
import re
from difflib import SequenceMatcher
from fastapi import FastAPI, Body, Depends, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
import uvicorn
from typing import Optional
import os
# ========== Search/Replace-Block Approach with Improved Matching ==========
class SearchReplaceResponse(BaseModel):
    """Schema for the LLM response."""
    search_replace: str = Field(description="The code containing SEARCH/REPLACE blocks.")
    explanation: str = Field(description="Explanation of the changes made to the code.")

class AssetLinkRequest(BaseModel):
    """Schema for the API request with asset link."""
    input_code_file: str = Field(description="The source code to modify.")
    user_prompt: str = Field(description="The user's prompt describing the desired changes.")
    asset_url: Optional[str] = Field(None, description="Optional URL to an asset to include in the code.")

class PlanResponse(BaseModel):
    """Schema for the LLM-generated plan."""
    plan: str = Field(description="The structured plan describing all changes to be made.")

def extract_asset_url_from_prompt(prompt: str) -> Optional[str]:
    """
    Extract asset URL from a prompt using various regex patterns.
    Returns the first matching URL or None if no URL is found.
    """
    # Enhanced patterns for asset URLs in prompts
    url_patterns = [
        r'asset (?:url|link)[\s:]+([^\s,]+)',
        r'(?:image|icon|resource) (?:url|link)[\s:]+([^\s,]+)',
        r'(?:use|include|add) (?:the )?(?:asset|image|icon|resource)[\s:]+([^\s,]+)',
        r'(?:https?://[^\s,]+\.(?:png|jpg|jpeg|gif|svg|webp|mp3|mp4|wav|ogg))',
        r'(?:use|include|add|with) (?:this|the) (?:url|link)[\s:]+([^\s,]+)',
        r'(?:url|link)[\s:]+([^\s,]+\.(?:png|jpg|jpeg|gif|svg|webp|mp3|mp4|wav|ogg))'
    ]

    for pattern in url_patterns:
        matches = re.findall(pattern, prompt, re.IGNORECASE)
        if matches:
            extracted_url = matches[0]
            print(f"Extracted asset URL from prompt: {extracted_url}")
            return extracted_url

    return None

def generate_search_replace_blocks(plan: str, source_code: str, asset_url: Optional[str] = None) -> tuple:
    """
    Generate SEARCH/REPLACE blocks based on the task plan, source code, and optional asset URL.
    Asset URL can be provided directly or extracted from the prompt.
    """
    # Check if there's an asset URL in the prompt if none was explicitly provided
    if not asset_url:
        asset_url = extract_asset_url_from_prompt(plan)

    asset_instruction = ""
    if asset_url:
        asset_instruction = f"""
        IMPORTANT: Include the following asset URL in your implementation: {asset_url}
        Make sure to properly integrate this asset into the code according to the user's request.
        The asset URL should be used appropriately based on its file type (image, audio, etc.).
        """

    modification_prompt = f"""
    Based on the following plan:
    {plan}
    Modify the following source code accordingly:
    ```js
    {source_code}
    ```
    {asset_instruction}
    Ensure this format
    <<<<<<< SEARCH
     Exact original lines
    =======
     updated lines
    >>>>>>> REPLACE
    You can delete code by replacing it with newline and showing the code which needs to be worked on
    Output SEARCH/REPLACE blocks for each task. Ensure all changes are made comprehensively and provide explanations.
    """

    try:
        instructor_client = instructor.from_anthropic(
            anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "")),
        )
        response = instructor_client.chat.completions.create(
            model="claude-3-5-sonnet-latest",
            response_model=SearchReplaceResponse,
            max_tokens=8192,
            messages=[
                {"role": "system", "content": "You are an expert js code modifier.'Please do ensure that Scope and Context Issue: function is defined outside the class'  doesnt happen"},
                {"role": "system", "content": """Ensure this format is followed for all blocks
        <<<<<<< SEARCH
         original lines
        =======
         updated lines
        >>>>>>> REPLACE"""},
                {"role": "user", "content": modification_prompt},
            ],
        )
        print(response.search_replace)
        return response.search_replace, response.explanation
    except Exception as e:
        print(f"Error generating code modifications: {str(e)}")
        raise e
def parse_search_replace_blocks(content: str):
    """
    Parse the AI's response for SEARCH/REPLACE blocks of the form:
    <<<<<<< SEARCH
    ... original lines ...
    =======
    ... updated lines ...
    >>>>>>> REPLACE
    Returns a list of tuples (original_text, updated_text).
    """
    HEAD = r"^<{5,9} SEARCH\s*$"
    DIVIDER = r"^={5,9}\s*$"
    UPDATED = r"^>{5,9} REPLACE\s*$"
    head_pattern = re.compile(HEAD, re.MULTILINE)
    divider_pattern = re.compile(DIVIDER, re.MULTILINE)
    updated_pattern = re.compile(UPDATED, re.MULTILINE)
    blocks = []
    lines = content.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if head_pattern.match(line.strip()):
            i += 1
            original_text_lines = []
            while i < len(lines) and not divider_pattern.match(lines[i].strip()):
                original_text_lines.append(lines[i])
                i += 1
            if i >= len(lines) or not divider_pattern.match(lines[i].strip()):
                raise ValueError("Malformed block: missing '=======' divider.")
            i += 1
            updated_text_lines = []
            while i < len(lines) and not updated_pattern.match(lines[i].strip()):
                updated_text_lines.append(lines[i])
                i += 1
            if i >= len(lines) or not updated_pattern.match(lines[i].strip()):
                raise ValueError("Malformed block: missing '>>>>>>> REPLACE' delimiter.")
            original_text = "".join(original_text_lines)
            updated_text = "".join(updated_text_lines)
            blocks.append((original_text, updated_text))
        i += 1
    return blocks
def apply_search_replace_blocks(source_code: str, blocks):
    """
    Apply each SEARCH/REPLACE block to the source code in memory.
    """
    updated_code = source_code
    for original_text, updated_text in blocks:
        if original_text in updated_code:
            updated_code = updated_code.replace(original_text, updated_text)
        else:
            print(f"Warning: Failed to apply block. Original text not found:\n{original_text}")
    return updated_code
def debug_applied_changes(source_code: str, blocks):
    """
    Debugging function to log changes step-by-step.
    """
    print("Initial Source Code:")
    print(source_code)
    updated_code = source_code
    for i, (original_text, updated_text) in enumerate(blocks, start=1):
        print(f"\n--- Applying Block {i} ---")
        print(f"SEARCH:\n{original_text}")
        print(f"REPLACE:\n{updated_text}")
        if original_text in updated_code:
            updated_code = updated_code.replace(original_text, updated_text)
            print("Block applied successfully.")
        else:
            print("Warning: Original text not found. Block skipped.")
    print("\nFinal Updated Code:")
    print(updated_code)
    return updated_code
def format_code_with_prettier(code: str) -> str:
    """
    Format JavaScript code using Prettier.
    """
    try:
        process = subprocess.run(
            ["prettier", "--parser", "babel"],
            input=code,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            raise ValueError(f"Prettier error: {process.stderr.strip()}")
        return process.stdout
    except FileNotFoundError:
        raise RuntimeError("Prettier is not installed or not found in PATH.")
def validate_final_code_with_esprima(code: str) -> bool:
    """
    Validate JavaScript code syntax using esprima.
    """
    try:
        process = subprocess.run(
            ["esvalidate", "--"],
            input=code,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            print(f"Esprima validation failed: {process.stderr.strip()}")
            return False
        return True
    except FileNotFoundError:
        raise RuntimeError("Esprima is not installed or not found in PATH.")
def main():
    st.title("js Code Modification Tool")
    st.write("Provide a prompt and source code. The AI will produce SEARCH/REPLACE blocks to modify your Phaser.js code.")

    prompt = st.text_area("Enter your modification prompt:", "")
    source_code = st.text_area("Enter the source code:", "")
    asset_url = st.text_input("Asset URL (optional):", "")
    st.info("You can also include asset URLs directly in your prompt.")

    if st.button("Generate Code Changes"):
        if not prompt or not source_code:
            st.error("Please provide both a prompt and source code.")
        else:
            with st.spinner("Generating SEARCH/REPLACE blocks..."):
                try:
                    # Check if there's an asset URL in the prompt if none was explicitly provided
                    extracted_asset_url = asset_url
                    if not extracted_asset_url:
                        extracted_asset_url = extract_asset_url_from_prompt(prompt)
                        if extracted_asset_url:
                            st.info(f"Extracted asset URL from prompt: {extracted_asset_url}")

                    search_replace_blocks, exp = generate_search_replace_blocks(prompt, source_code, extracted_asset_url)
                    st.subheader("Generated SEARCH/REPLACE Blocks")
                    st.code(search_replace_blocks, language="js")
                    st.code(exp)
                    try:
                        parsed_blocks = parse_search_replace_blocks(search_replace_blocks)
                        debug_applied_changes(source_code, parsed_blocks)  # Debugging added
                        updated_code = apply_search_replace_blocks(source_code, parsed_blocks)
                        if validate_final_code_with_esprima(updated_code):
                            formatted_code = format_code_with_prettier(updated_code)
                            st.success("All tasks applied and syntax validated successfully!")
                            st.subheader("Updated Code")
                            st.code(formatted_code, language="js")
                        else:
                            st.error("Syntax validation failed.")
                    except ValueError as e:
                        st.warning(f"Validation or application issues: {e}")
                except Exception as e:
                    st.error(f"Error generating code changes: {e}")

# API Key configuration
API_KEY = os.environ.get("API_KEY", "test-api-key")  # Default key for testing
API_KEY_NAME = "x-api-key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header == API_KEY:
        return api_key_header
    raise HTTPException(
        status_code=401,
        detail="Invalid API Key",
    )

# Create FastAPI app
app = FastAPI(
    title="Aicade Code Modification API",
    description="API for modifying JavaScript code with asset link integration",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)



@app.post("/generate-code/")
async def generate_code(request: AssetLinkRequest, _: str = Depends(get_api_key)):
    """
    API endpoint to generate code changes with optional asset link integration.

    Parameters:
    - input_code_file: The source code to modify
    - user_prompt: The user's prompt describing the desired changes
    - asset_url: Optional URL to an asset to include in the code
      (Can also be specified directly in the user_prompt)

    Returns:
    - search_replace: The code containing SEARCH/REPLACE blocks
    - explanation: Explanation of the changes made to the code
    - updated_code: The final updated code after applying the changes
    - detected_asset_url: The asset URL that was used (either from asset_url parameter or extracted from prompt)
    """
    try:
        # First, check if there's an asset URL in the prompt if none was explicitly provided
        asset_url = request.asset_url
        if not asset_url:
            asset_url = extract_asset_url_from_prompt(request.user_prompt)
            if asset_url:
                print(f"API: Extracted asset URL from prompt: {asset_url}")

        search_replace, explanation = generate_search_replace_blocks(
            request.user_prompt,
            request.input_code_file,
            asset_url
        )

        parsed_blocks = parse_search_replace_blocks(search_replace)
        updated_code = apply_search_replace_blocks(request.input_code_file, parsed_blocks)

        # Try to format the code if possible
        try:
            if validate_final_code_with_esprima(updated_code):
                updated_code = format_code_with_prettier(updated_code)
        except Exception:
            # If formatting fails, return the unformatted code
            pass

        return {
            "search_replace": search_replace,
            "explanation": explanation,
            "updated_code": updated_code,
            "detected_asset_url": asset_url
        }
    except Exception as e:
        error_message = f"Error code: {getattr(e, 'status_code', 500)} - {str(e)}"
        return {"error": error_message}

# Run the app with both Streamlit and FastAPI
if __name__ == "__main__":
    import threading

    # Run FastAPI in a separate thread
    def run_fastapi():
        uvicorn.run(app, host="0.0.0.0", port=8000)

    # Start FastAPI server in a separate thread
    threading.Thread(target=run_fastapi, daemon=True).start()

    # Run Streamlit app
    main()
