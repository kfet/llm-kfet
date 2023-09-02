from .models import EmbeddingModel
from .embeddings_migrations import embeddings_migrations
from dataclasses import dataclass
from itertools import islice
import json
from sqlite_utils import Database
from sqlite_utils.db import Table
from typing import cast, Any, Dict, Iterable, List, Optional, Union


@dataclass
class Entry:
    id: str
    score: Optional[float]
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class Collection:
    max_batch_size: int = 100

    def __init__(
        self,
        db: Database,
        name: str,
        *,
        model: Optional[EmbeddingModel] = None,
        model_id: Optional[str] = None,
    ) -> None:
        self.db = db
        self.name = name
        if model and model_id and model.model_id != model_id:
            raise ValueError("model_id does not match model.model_id")
        self._model = model
        self._model_id = model_id
        self._id = None
        self._id = self.id()

    def model(self) -> EmbeddingModel:
        import llm

        if self._model:
            return self._model
        try:
            if not self._model_id:
                raise ValueError("No model_id specified")
            self._model = llm.get_embedding_model(self._model_id)
        except llm.UnknownModelError:
            raise ValueError("No model_id specified and no model found with that name")
        return cast(EmbeddingModel, self._model)

    def id(self) -> int:
        """
        Get the ID of the collection, creating it in the DB if necessary.

        Returns:
            int: ID of the collection
        """
        if self._id is not None:
            return self._id
        if not self.db["collections"].exists():
            embeddings_migrations.apply(self.db)
        rows = self.db["collections"].rows_where("name = ?", [self.name])
        try:
            row = next(rows)
            self._id = row["id"]
            if self._model_id is None:
                self._model_id = row["model"]
        except StopIteration:
            # Create it
            self._id = (
                cast(Table, self.db["collections"])
                .insert(
                    {
                        "name": self.name,
                        "model": self.model().model_id,
                    }
                )
                .last_pk
            )
        return cast(int, self._id)

    def exists(self) -> bool:
        """
        Check if the collection exists in the DB.

        Returns:
            bool: True if exists, False otherwise
        """
        matches = list(
            self.db.query("select 1 from collections where name = ?", (self.name,))
        )
        return bool(matches)

    def count(self) -> int:
        """
        Count the number of items in the collection.

        Returns:
            int: Number of items in the collection
        """
        return next(
            self.db.query(
                """
            select count(*) as c from embeddings where collection_id = (
                select id from collections where name = ?
            )
            """,
                (self.name,),
            )
        )["c"]

    def embed(
        self,
        id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        store: bool = False,
    ) -> None:
        """
        Embed a text and store it in the collection with a given ID.

        Args:
            id (str): ID for the text
            text (str): Text to be embedded
            metadata (dict, optional): Metadata to be stored
            store (bool, optional): Whether to store the text in the content column
        """
        from llm import encode

        embedding = self.model().embed(text)
        cast(Table, self.db["embeddings"]).insert(
            {
                "collection_id": self.id(),
                "id": id,
                "embedding": encode(embedding),
                "content": text if store else None,
                "metadata": json.dumps(metadata) if metadata else None,
            }
        )

    def embed_multi(
        self, entries: Iterable[Union[str, str]], store: bool = False
    ) -> None:
        """
        Embed multiple texts and store them in the collection with given IDs.

        Args:
            entries (iterable): Iterable of (id: str, text: str) tuples
            store (bool, optional): Whether to store the text in the content column
        """
        self.embed_multi_with_metadata(
            ((id, text, None) for id, text in entries), store=store
        )

    def embed_multi_with_metadata(
        self,
        entries: Iterable[Union[str, str, Optional[Dict[str, Any]]]],
        store: bool = False,
    ) -> None:
        """
        Embed multiple texts along with metadata and store them in the collection with given IDs.

        Args:
            entries (iterable): Iterable of (id: str, text: str, metadata: None or dict)
            store (bool, optional): Whether to store the text in the content column
        """
        import llm

        batch_size = min(
            self.max_batch_size, (self.model().batch_size or self.max_batch_size)
        )
        iterator = iter(entries)
        collection_id = self.id()
        while True:
            batch = list(islice(iterator, batch_size))
            if not batch:
                break
            embeddings = list(self.model().embed_multi(item[1] for item in batch))
            with self.db.conn:
                cast(Table, self.db["embeddings"]).insert_all(
                    (
                        {
                            "collection_id": collection_id,
                            "id": id,
                            "embedding": llm.encode(embedding),
                            "content": text if store else None,
                            "metadata": json.dumps(metadata) if metadata else None,
                        }
                        for (embedding, (id, text, metadata)) in zip(embeddings, batch)
                    )
                )

    def similar_by_vector(
        self, vector: List[float], number: int = 10, skip_id: Optional[str] = None
    ) -> List[Entry]:
        """
        Find similar items in the collection by a given vector.

        Args:
            vector (list): Vector to search by
            number (int, optional): Number of similar items to return

        Returns:
            list: List of Entry objects
        """
        import llm

        def distance_score(other_encoded):
            other_vector = llm.decode(other_encoded)
            return llm.cosine_similarity(other_vector, vector)

        self.db.register_function(distance_score, replace=True)

        where_bits = ["collection_id = ?"]
        where_args = [str(self.id())]

        if skip_id:
            where_bits.append("id != ?")
            where_args.append(skip_id)

        return [
            Entry(
                id=row["id"],
                score=row["score"],
                content=row["content"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else None,
            )
            for row in self.db.query(
                """
            select id, content, metadata, distance_score(embedding) as score
            from embeddings
            where {where}
            order by score desc limit {number}
        """.format(
                    where=" and ".join(where_bits),
                    number=number,
                ),
                where_args,
            )
        ]

    def similar_by_id(self, id: str, number: int = 10) -> List[Entry]:
        """
        Find similar items in the collection by a given ID.

        Args:
            id (str): ID to search by
            number (int, optional): Number of similar items to return

        Returns:
            list: List of Entry objects
        """
        import llm

        matches = list(
            self.db["embeddings"].rows_where(
                "collection_id = ? and id = ?", (self.id(), id)
            )
        )
        if not matches:
            raise ValueError("ID not found")
        embedding = matches[0]["embedding"]
        comparison_vector = llm.decode(embedding)
        return self.similar_by_vector(comparison_vector, number, skip_id=id)

    def similar(self, text: str, number: int = 10) -> List[Entry]:
        """
        Find similar items in the collection by a given text.

        Args:
            text (str): Text to search by
            number (int, optional): Number of similar items to return

        Returns:
            list: List of Entry objects
        """
        comparison_vector = self.model().embed(text)
        return self.similar_by_vector(comparison_vector, number)