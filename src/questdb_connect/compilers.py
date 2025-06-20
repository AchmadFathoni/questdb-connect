import abc

import sqlalchemy
from sqlalchemy.sql.base import elements

from .common import quote_identifier, remove_public_schema
from .types import QDBTypeMixin


class QDBDDLCompiler(sqlalchemy.sql.compiler.DDLCompiler, abc.ABC):
    def visit_create_schema(self, create, **kw):
        raise Exception("QuestDB does not support SCHEMAS, there is only 'public'")

    def visit_drop_schema(self, drop, **kw):
        raise Exception("QuestDB does not support SCHEMAS, there is only 'public'")

    def visit_create_table(self, create, **kw):
        table = create.element
        create_table = f"CREATE TABLE {quote_identifier(table.fullname)} ("
        create_table += ", ".join(
            [self.get_column_specification(c.element) for c in create.columns]
        )
        return create_table + ") " + table.engine.get_table_suffix()

    def get_column_specification(self, column: sqlalchemy.Column, **_):
        if not isinstance(column.type, QDBTypeMixin):
            raise sqlalchemy.exc.ArgumentError(
                "Column type is not a valid QuestDB type"
            )
        return column.type.column_spec(column.name)


class QDBSQLCompiler(sqlalchemy.sql.compiler.SQLCompiler, abc.ABC):
    # Maximum value for 64-bit signed integer (2^63 - 1)
    BIGINT_MAX = 9223372036854775807

    def visit_sample_by(self, sample_by, **kw):
        """Compile a SAMPLE BY clause."""
        text = ""

        # Basic SAMPLE BY
        if sample_by.unit:
            text = f"SAMPLE BY {sample_by.value}{sample_by.unit}"
        else:
            text = f"SAMPLE BY {sample_by.value}"

        if sample_by.from_timestamp:
            # Format datetime to ISO format that QuestDB expects
            text += f" FROM '{sample_by.from_timestamp.isoformat()}'"
        if sample_by.to_timestamp:
            text += f" TO '{sample_by.to_timestamp.isoformat()}'"

        # Add FILL if specified
        if sample_by.fill is not None:
            if isinstance(sample_by.fill, str):
                text += f" FILL({sample_by.fill})"
            else:
                text += f" FILL({sample_by.fill:g})"

        # Add ALIGN TO clause
        text += f" ALIGN TO {sample_by.align_to}"

        # Add TIME ZONE if specified
        if sample_by.timezone:
            text += f" TIME ZONE '{sample_by.timezone}'"

        # Add WITH OFFSET if specified
        if sample_by.offset:
            text += f" WITH OFFSET '{sample_by.offset}'"

        return text

    def group_by_clause(self, select, **kw):
        """Customize GROUP BY to also render SAMPLE BY."""
        text = ""

        # Add SAMPLE BY first if present
        if _has_sample_by(select):
            text += " " + self.process(select._sample_by_clause, **kw)

        # Use parent's GROUP BY implementation
        group_by_text = super().group_by_clause(select, **kw)
        if group_by_text:
            text += group_by_text

        return text

    def visit_select(self, select, **kw):
        """Add SAMPLE BY support to the standard SELECT compilation."""

        # If we have SAMPLE BY but no GROUP BY,
        # add a dummy GROUP BY clause to trigger the rendering
        if (
                _has_sample_by(select)
                and not select._group_by_clauses
        ):
            select = select._clone()
            select._group_by_clauses = [elements.TextClause("")]

        text = super().visit_select(select, **kw)
        return text

    def _is_safe_for_fast_insert_values_helper(self):
        return True

    def visit_textclause(self, textclause, add_to_result_map=None, **kw):
        textclause.text = remove_public_schema(textclause.text)
        return super().visit_textclause(textclause, add_to_result_map, **kw)

    def limit_clause(self, select, **kw):
        """
        Generate QuestDB-style LIMIT clause from SQLAlchemy select statement.
        QuestDB supports arbitrary expressions in LIMIT clause.
        """
        text = ""
        limit = select._limit_clause
        offset = select._offset_clause

        if limit is None and offset is None:
            return text

        text += "\n LIMIT "

        # Handle cases based on presence of limit and offset
        if limit is not None and offset is not None:
            # Convert LIMIT x OFFSET y to LIMIT y,y+x
            lower_bound = self.process(offset, **kw)
            limit_val = self.process(limit, **kw)
            text += f"{lower_bound},{lower_bound} + {limit_val}"

        elif limit is not None:
            text += self.process(limit, **kw)

        elif offset is not None:
            # If only offset is specified, use max bigint as upper bound
            text += f"{self.process(offset, **kw)},{self.BIGINT_MAX}"

        return text

def _has_sample_by(select):
    return hasattr(select, '_sample_by_clause') and select._sample_by_clause is not None