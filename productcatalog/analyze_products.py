import getpass
import os
import argparse
from typing_extensions import Annotated, TypedDict, List
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.utilities import SQLDatabase
from langchain_community.tools.sql_database.tool import QuerySQLDatabaseTool
from langgraph.graph import START, StateGraph
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langgraph.prebuilt import create_react_agent
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain import hub
from langchain_core.documents import Document
from langgraph.checkpoint.memory import MemorySaver


PARSER = argparse.ArgumentParser(description="command line flags.")
PARSER.add_argument('--user', type=str, help='db user')
PARSER.add_argument('--pwd', type=str, help='db password')
PARSER.add_argument('--db', type=str, help='db name')
PARSER.add_argument('--advanced', type=bool, default=False, help='if set use agent for deeper search, else use workflow')
PARSER.add_argument('--topk', type=bool, default=10, help='max count of rows in the db to be processed')
ARGS = PARSER.parse_args()
LLM_VERSION = "gemini-2.0-flash-lite"
LLM_PROVIDER = "google_genai"
POSTGRESQL_ADDRESS = "localhost:5432"


class ModelAdapter:

    def _init_db(self):
        self.db = SQLDatabase.from_uri(f"postgresql://{ARGS.user}:{ARGS.pwd}@{POSTGRESQL_ADDRESS}/{ARGS.db}")

    def _init_llm(self):
        if not os.environ.get("GOOGLE_API_KEY"):
            raise Exception("API key missing for Google Gemini")
        self.model = init_chat_model(LLM_VERSION, model_provider=LLM_PROVIDER)

    def _init_prompt_config(self):
        system_template = """
            Given your previous conversations with user in chronological order
            and user's current question, based on user's intent create a syntactically 
            correct {dialect} query to run to help find the answer. If the user's question is about
            a specific product or set of products then build db query only for that product(s) not every product.

            Additionally, unless the user specifies in his question a specific number of examples 
            they wish to obtain, always limit your query to at most {top_k} results. You can order 
            the results by a relevant column to return the most interesting examples in the database.
            Never query for all the columns from a specific table, only ask for a the
            few relevant columns given the question.
            Pay attention to use only the column names that you can see in the schema
            description. Be careful to not query for columns that do not exist. Also,
            pay attention to which column is in which table.
            Only use the following tables:
            {table_info}
            """
        self.prompt_template = ChatPromptTemplate.from_messages(
            [("system", system_template), ("user", """
                Our previous conversations: {message_history}
                My current question: {input}
                """)]
        )

    def _init_agent(self):
        toolkit = SQLDatabaseToolkit(db=self.db, llm=self.model)
        self.tools = toolkit.get_tools()
        system_message = """
            You are an agent designed to interact with a SQL database.
            Given an input question, create a syntactically correct {dialect} query to run,
            then look at the results of the query and return the answer. Unless the user
            specifies a specific number of examples they wish to obtain, always limit your
            query to at most {top_k} results.

            You can order the results by a relevant column to return the most interesting
            examples in the database. Never query for all the columns from a specific table,
            only ask for the relevant columns given the question.

            You MUST double check your query before executing it. If you get an error while
            executing a query, rewrite the query and try again.

            DO NOT make any DML statements (INSERT, UPDATE, DELETE, DROP etc.) to the
            database.

            To start you should ALWAYS look at the tables in the database to see what you
            can query. Do NOT skip this step.

            Then you should query the schema of the most relevant tables. Your final response
            should be as if you're the vendor of the products.
            """.format(
                dialect=self.db.dialect,
                top_k=ARGS.topk,
            )
        self.agent = create_react_agent(self.model, self.tools, prompt=system_message)

    def _init_vector_store(self):
        self.embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
        self.vector_store = InMemoryVectorStore(self.embeddings) # efficient for embedded vectors operations

    def _load_documents(self):
        loader = TextLoader("productcatalog/resources/product-feedbacks.txt", encoding="utf-8")
        docs = loader.load()
        text_splitter = RecursiveCharacterTextSplitter()
        self.all_splits = text_splitter.split_documents(docs)

    def _index_documents(self):
        _ = self.vector_store.add_documents(documents=self.all_splits)
        self.rag_prompt_template = hub.pull("rlm/rag-prompt")

    class DBQuery(TypedDict):
        """Generated SQL query."""
        query: Annotated[str, ..., "Valid SQL query."]

    class RagState(TypedDict):
        question: str
        db_query: str
        db_result: str
        db_response: str
        context: List[Document]
        rag_response: str
        config: dict[str,Any]
        answer: str
        messages: [] # to maintain the context

    def _build_message_history(self, state:RagState):
        messages = state.get("messages", [])
        history = ""
        for msg in messages[:-1]:
            role = msg.get("role", "unknown").capitalize()
            content = msg.get("content", "")
            history += f"{role}: {content}\n"
        return history.strip()

    def _build_query(self, state:RagState):
        if state['config']['advanced']:
            return state
        prompt = self.prompt_template.invoke({
            "dialect": self.db.dialect, 
            "top_k": state["config"]["top_k"], 
            "table_info": self.db.get_table_info(), 
            "message_history": self._build_message_history(state),
            "input": state["question"]})
        self.structured_model = self.model.with_structured_output(self.DBQuery)
        response = self.structured_model.invoke(prompt)
        return {"db_query": response["query"]}

    def _execute_query(self, state: RagState):
        if state['config']['advanced']:
            return state
        self._execute_query_tool = QuerySQLDatabaseTool(db=self.db)
        db_result = self._execute_query_tool.invoke(state["db_query"])
        return {"db_result": db_result}

    def _build_answer(self, state:RagState):
        advanced_mode = state['config']['advanced']
        if advanced_mode:
            return state
        prompt = (
            "Given the following user question, corresponding SQL query, and SQL result, answer in short the user question as the vendor of products.\n\n"
            f"Question: {state['question']}\n"
            f"SQL Query: {state['db_query']}\n"
            f"SQL Result: {state['db_result']}"
        )
        response = self.model.invoke(prompt)
        return {'db_response': response.content}

    def _build_search_query_with_context(self, state:RagState):
        system_template = """
            Given your previous conversation with user about a product(s). Extract the product name from it and respond a message
            strictly in the format 'product feedback for products (product_name)' where (product_name) is the product
            mentioned in your given response. If there's no valid product name extractable then (product name) should be just ''.
            """            
        prompt_template = ChatPromptTemplate.from_messages(
            [("system", system_template), ("user", "Our previous conversations: {conversations}")]
        )
        prompt = prompt_template.invoke({'conversations': self._build_message_history(state)})
        response = self.model.invoke(prompt)
        print(response.content)
        return response.content

    def _retrive_docs(self, state:RagState):
        retrieved_docs = self.vector_store.similarity_search(self._build_search_query_with_context(state))
        return {"context": retrieved_docs}

    def _generate_from_context(self, state:RagState):
        docs_content = "\n\n".join(doc.page_content for doc in state["context"])
        messages = self.rag_prompt_template.invoke({"question": state["question"], "context": docs_content})
        response = self.model.invoke(messages)
        return {"rag_response": response.content}

    def _merge_smart(self, state:RagState):
        system_template = """
            Given your previous conversations with the user in chronological order, user's current question, 
            tool's response from the db about product details which may have any possible relevance to user's current question,
            tool's response from text data via RAG about feedbacks on the products that might have any possible relevance to user's 
            current question. Based on these, answer in short to the user question as a vendor of the products.

            While answering incorporate a tool's response if it has actual relevance to user's intent since some tool responses
            mayn't have relevance to user's current question in some scenarios.
            """
        prompt_template = ChatPromptTemplate.from_messages(
            [("system", system_template), ("user", """
                Our previous conversations: {message_history}
                My current question: {input}
                Tool response to my current question based on DB data: {db_response}
                Tool response to my current question based on text data: {rag_response}
                """)]
        )
        prompt = prompt_template.invoke({
            "message_history": self._build_message_history(state),
            "input": state["question"],
            "db_response": state["db_response"],
            "rag_response": state["rag_response"]
            })
        response = self.model.invoke(prompt)
        state['messages'].append(AIMessage(content=response.content))
        return {"answer": response.content}

    def _search_db(self, state:RagState):
        # heavy retrial mechanism
        for step in self.agent.stream(
            {"messages": [{"role": "user", "content": state['question']}]},
            stream_mode="values",
        ):
            response = step["messages"][-1]
        db_response = response.content
        return {'db_response': db_response}

    def _build_rag_workflow(self):
        graph_builder = StateGraph(self.RagState)
        graph_builder.add_sequence([self._retrive_docs, self._generate_from_context, self._merge_smart])
        graph_builder.add_sequence([self._build_query, self._execute_query, self._build_answer])
        graph_builder.add_node(self._search_db)
        graph_builder.add_edge("_search_db", "_retrive_docs")
        graph_builder.add_edge("_build_answer", "_retrive_docs")
        graph_builder.add_conditional_edges(START, lambda x: x["config"]["advanced"], {
            True: "_search_db",
            False: "_build_query"
        })
        self.message_memory = MemorySaver()
        self.message_thread_config = {"configurable":{"thread_id":"common"}}
        self.rag_graph = graph_builder.compile(checkpointer=self.message_memory)

    def __init__(self):
        self._init_db()
        self._init_vector_store()
        self._load_documents()
        self._index_documents()

        self._init_llm()
        self._init_prompt_config()
        self._init_agent()

        self._build_rag_workflow()

    def invoke(self, state:RagState):
        state["messages"] = [HumanMessage(content=question)]
        response = self.rag_graph.invoke(state, self.message_thread_config) # use persistent context by default
        return response


adapter = ModelAdapter()
while True:
    question = input("> ")
    response = adapter.invoke({"question":question,"config":{"top_k":ARGS.topk, "advanced":ARGS.advanced}})
    print(response['answer'])