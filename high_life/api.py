import re
import sqlite3
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from llama_index.core import PromptTemplate
from llama_index.core.schema import NodeWithScore, MetadataMode
from llama_index.llms.ollama import Ollama
from pydantic import BaseModel
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

from search import log_retrieval, retrieve_documents, gather_nodes_recursively
from settings import logging_startup

logging_startup()
app = FastAPI()
app.mount("/src", StaticFiles(directory="src"), name="src")

origins = [
    "http://localhost:8002",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def root():
    with open("src/index.html") as f:
        return HTMLResponse(content=f.read(), status_code=200)


class Input(BaseModel):
    text: str


def log_query(logging_id: str, query_str: str):
    with sqlite3.connect('logs.sqlite3') as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO queries (logging_id,query) VALUES (?,?)", (logging_id, query_str))


@app.post("/query")
async def query(input: Input) -> dict[str, str]:
    logging_id: str = uuid.uuid4().hex
    return {"text": master_query(input.text, logging_id)}


prompt = PromptTemplate(""" You are the world's best recommendation bot designed to answer user questions about food, travel, wine, etc.
    We have provided you a document to answer a query from an search embedding search.
    Your job is to determine whether the subject of document answers the query given the proper keywords or topics.
    if some asks for a red wine rec, only affirm red wines.
    if some asks about a city make sure the city is correct in the given document.
    QUERY: {query_str}
    ------------ Document ------------
    {doc}
    ----------------------------------
    ------------Output format------------
     Before answering the question, please think about it step-by-step within <thinking></thinking> tags.
     Then, provide your final answer within <answer>Yes/No</answer> tags.
    """)

accumulated_prompt = PromptTemplate("""You are the worlds best Q&A bot, specializing in synthesizing correct answers to questions.
    You will be given a knowledge base that has been proven to answer the user query.
    You're job is to extract the text that best answers the query and give it to the user.
    -----------------
    KNOWLEDGE_BASE: {docs}
    -----------------
    QUERY: {query_str}
    -----------------
    <<<RESPONSE INSTRUCTIONS: If the documents do not contain a suitable answer, simply respond: "i could not find a suitable answer".
    Do NOT suggest a solution from your own knowledge.
    Do NOT include phrases like 'Based on the provided documents, or 'According to the documents'. or  'based on the documents provided.'
    Include the url from the document metadata.
    >>>>
    ------------------
    Before answering the question, please think about it step-by-step within <thinking></thinking> tags.
    Then, provide your final answer within <answer></answer> tags.
    """)


def log_filtering_responses(logging_id, query_str: str, model_resp: str):
    with sqlite3.connect('logs.sqlite3') as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO filtering_responses (logging_id,input,result) VALUES (?,?,?)",
                       (logging_id, query_str, model_resp))


def log_final_responses(logging_id, query_str: str, model_resp: str):
    with sqlite3.connect('logs.sqlite3') as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO final_responses (logging_id,input,result) VALUES (?,?,?)",
                       (logging_id, query_str, model_resp))


def master_query(query_str: str, logging_id) -> str:
    log_query(logging_id, query_str=query_str)
    docs: list[NodeWithScore] = retrieve_documents(query_str=query_str)  # + vector_retrieved_docs2
    log_retrieval(logging_id, docs)
    texts = gather_nodes_recursively(docs)
    _answers = []
    model = Ollama(model="mistral", temperature=0, request_timeout=500, )
    for nodes in texts.values():
        doc = " ".join([node.get_content(metadata_mode=MetadataMode.NONE) for node in nodes])
        resp = model.complete(prompt.format(query_str=query_str, doc=doc))
        log_filtering_responses(logging_id, doc, resp.text)
        _answers.extend(
            nodes
            for i in re.finditer(
                r"<answer>(?P<answer>.*)<\/answer>", resp.text.lower()
            )
            if "yes" in i.groupdict()["answer"]
        )
    answer_content = ""
    for index, node_list in enumerate(_answers):
        _answer_content = f"Document {index}:\n"
        for node in node_list:
            _answer_content += f" {node.get_content(metadata_mode=MetadataMode.NONE)}"
        answer_content += _answer_content
    if not answer_content:
        return "I could not find a suitable answer"
    final_prompt = accumulated_prompt.format(
        query_str=query_str,
        docs=answer_content
    )
    final_response = Ollama(model="mistral", temperature=0.1, request_timeout=500).complete(final_prompt).text
    log_final_responses(logging_id, final_prompt, final_response)

    for i in re.finditer(r"<answer>(?P<answer>.*)<\/answer>", final_response.replace("\n", " ")):
        return i.groupdict()["answer"]
