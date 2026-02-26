"""ClickHouse query translator — replaces ast_druid_translator.py for Divine's stack.

Converts Osprey's query AST into ClickHouse SQL WHERE clauses instead of
Druid JSON filter objects. The AST nodes are the same; only the output
format changes.
"""

from typing import Any

from osprey.engine.ast import grammar
from osprey.engine.ast_validator.validation_context import ValidatedSources
from osprey.engine.ast_validator.validators.validate_call_kwargs import ValidateCallKwargs
from osprey.engine.udf.base import QueryUdfBase
from osprey.engine.utils.osprey_unary_executor import OspreyUnaryExecutor


class ClickHouseQueryTransformException(Exception):
    def __init__(self, node: grammar.ASTNode, error: str):
        super().__init__(f'{error}: {node.__class__.__name__}')
        self.node = node


class ClickHouseQueryTransformer:
    """Given an Osprey AST node tree, transform it into a ClickHouse SQL WHERE clause."""

    def __init__(self, validated_sources: ValidatedSources):
        try:
            self._udf_node_mapping = validated_sources.get_validator_result(ValidateCallKwargs)
        except KeyError:
            self._udf_node_mapping = {}

        assign_node = validated_sources.sources.get_entry_point().ast_root.statements[0]
        assert isinstance(assign_node, grammar.Assign)
        self._root = assign_node.value

    def transform(self) -> str:
        """Returns a SQL WHERE clause string (without the WHERE keyword)."""
        return self._transform(self._root)

    def _transform(self, node: grammar.ASTNode) -> str:
        method = 'transform_' + node.__class__.__name__
        transformer = getattr(self, method, None)

        if not transformer:
            raise ClickHouseQueryTransformException(node, 'Unknown AST Expression')

        return transformer(node)

    def transform_BooleanOperation(self, node: grammar.BooleanOperation) -> str:
        assert isinstance(node.operand, (grammar.And, grammar.Or))

        operator = 'AND' if isinstance(node.operand, grammar.And) else 'OR'
        clauses = [self._transform(v) for v in node.values]
        joined = f' {operator} '.join(f'({c})' for c in clauses)
        return joined

    def transform_BinaryComparison(self, node: grammar.BinaryComparison) -> str:
        # Column-to-column comparison
        if isinstance(node.left, grammar.Name) and isinstance(node.right, grammar.Name):
            left_col = _quote_identifier(node.left.identifier)
            right_col = _quote_identifier(node.right.identifier)
            if isinstance(node.comparator, grammar.Equals):
                return f'{left_col} = {right_col}'
            elif isinstance(node.comparator, grammar.NotEquals):
                return f'{left_col} != {right_col}'
            else:
                raise ClickHouseQueryTransformException(
                    node.comparator, 'Column-to-column comparison only supports == and !='
                )

        dimension = _get_comparison_dimension(node)
        value = _get_comparison_value(node)
        col = _quote_identifier(dimension)

        if isinstance(node.comparator, grammar.Equals):
            if value is None:
                return f'{col} IS NULL'
            return f'{col} = {_format_value(value)}'

        elif isinstance(node.comparator, grammar.NotEquals):
            if value is None:
                return f'{col} IS NOT NULL'
            return f'{col} != {_format_value(value)}'

        elif isinstance(node.comparator, grammar.In):
            return _in_clause(col, value, negated=False)

        elif isinstance(node.comparator, grammar.NotIn):
            return _in_clause(col, value, negated=True)

        elif isinstance(node.comparator, grammar.LessThan):
            return f'{col} IS NOT NULL AND {col} < {_format_value(value)}'

        elif isinstance(node.comparator, grammar.LessThanEquals):
            return f'{col} IS NOT NULL AND {col} <= {_format_value(value)}'

        elif isinstance(node.comparator, grammar.GreaterThan):
            return f'{col} IS NOT NULL AND {col} > {_format_value(value)}'

        elif isinstance(node.comparator, grammar.GreaterThanEquals):
            return f'{col} IS NOT NULL AND {col} >= {_format_value(value)}'

        else:
            raise ClickHouseQueryTransformException(node.comparator, 'Unknown Binary Comparator')

    def transform_UnaryOperation(self, node: grammar.UnaryOperation) -> str:
        if isinstance(node.operator, grammar.Not):
            return f'NOT ({self._transform(node.operand)})'
        else:
            raise ClickHouseQueryTransformException(node, 'Unknown Unary Operator')

    def transform_Call(self, node: grammar.Call) -> str:
        udf, _ = self._udf_node_mapping[id(node)]

        if not isinstance(udf, QueryUdfBase):
            raise ClickHouseQueryTransformException(node, 'Unknown function call type')

        # QueryUdfBase subclasses need a to_clickhouse_query() method.
        # Fall back to to_druid_query() and attempt conversion if not available.
        if hasattr(udf, 'to_clickhouse_query'):
            return udf.to_clickhouse_query()

        # Best-effort: convert simple Druid selector filters
        druid_filter = udf.to_druid_query()
        return _druid_filter_to_sql(druid_filter)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quote_identifier(name: str) -> str:
    """Quote a column name for ClickHouse."""
    # Use backticks to safely quote identifiers that may contain special chars
    escaped = name.replace('`', '``')
    return f'`{escaped}`'


def _format_value(value: Any) -> str:
    """Format a Python value as a ClickHouse SQL literal."""
    if value is None:
        return 'NULL'
    elif isinstance(value, bool):
        return '1' if value else '0'
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, str):
        escaped = value.replace("'", "\\'")
        return f"'{escaped}'"
    elif isinstance(value, list):
        return f'({", ".join(_format_value(v) for v in value)})'
    else:
        escaped = str(value).replace("'", "\\'")
        return f"'{escaped}'"


def _in_clause(col: str, value: Any, negated: bool) -> str:
    """Build an IN or NOT IN clause, handling both list and string (LIKE) cases."""
    op = 'NOT IN' if negated else 'IN'

    if isinstance(value, str):
        # Druid's 'insensitive_contains' maps to ClickHouse ilike
        like_op = 'NOT ILIKE' if negated else 'ILIKE'
        escaped = value.replace('%', '\\%').replace('_', '\\_')
        return f"{col} {like_op} '%{escaped}%'"
    elif isinstance(value, list):
        formatted = ', '.join(_format_value(v) for v in value)
        return f'{col} {op} ({formatted})'
    else:
        return f'{col} = {_format_value(value)}' if not negated else f'{col} != {_format_value(value)}'


def _get_comparison_dimension(node: grammar.BinaryComparison) -> str:
    if isinstance(node.left, grammar.Name):
        return node.left.identifier
    elif isinstance(node.right, grammar.Name):
        return node.right.identifier
    else:
        raise ClickHouseQueryTransformException(node, 'Binary Comparator must contain at least one column')


def _get_comparison_value(node: grammar.BinaryComparison) -> Any:
    if isinstance(node.left, (grammar.Literal, grammar.UnaryOperation)):
        return _get_ast_node_value(node.left)
    elif isinstance(node.right, (grammar.Literal, grammar.UnaryOperation)):
        return _get_ast_node_value(node.right)
    return None


def _get_ast_node_value(node: grammar.ASTNode) -> Any:
    if isinstance(node, grammar.UnaryOperation):
        return OspreyUnaryExecutor(node).get_execution_value()
    elif isinstance(node, grammar.List):
        return [_get_ast_node_value(i) for i in node.items]
    elif isinstance(node, grammar.None_):
        return None
    elif isinstance(node, (grammar.String, grammar.Number, grammar.Boolean)):
        return node.value
    else:
        raise ClickHouseQueryTransformException(node, 'Node has no known value attribute')


def _druid_filter_to_sql(druid_filter: dict[str, Any]) -> str:
    """Best-effort conversion of a Druid JSON filter to SQL for UDF compatibility."""
    ftype = druid_filter.get('type', '')

    if ftype == 'selector':
        col = _quote_identifier(druid_filter['dimension'])
        val = druid_filter.get('value')
        if val is None:
            return f'{col} IS NULL'
        return f'{col} = {_format_value(val)}'

    elif ftype == 'not':
        inner = _druid_filter_to_sql(druid_filter['field'])
        return f'NOT ({inner})'

    elif ftype in ('and', 'or'):
        op = ftype.upper()
        parts = [_druid_filter_to_sql(f) for f in druid_filter['fields']]
        return f' {op} '.join(f'({p})' for p in parts)

    elif ftype == 'in':
        col = _quote_identifier(druid_filter['dimension'])
        vals = ', '.join(_format_value(v) for v in druid_filter['values'])
        return f'{col} IN ({vals})'

    elif ftype == 'bound':
        col = _quote_identifier(druid_filter['dimension'])
        parts = []
        if 'lower' in druid_filter:
            op = '>' if druid_filter.get('lowerStrict') else '>='
            parts.append(f'{col} {op} {_format_value(druid_filter["lower"])}')
        if 'upper' in druid_filter:
            op = '<' if druid_filter.get('upperStrict') else '<='
            parts.append(f'{col} {op} {_format_value(druid_filter["upper"])}')
        return ' AND '.join(parts) if parts else '1=1'

    elif ftype == 'columnComparison':
        dims = druid_filter['dimensions']
        return f'{_quote_identifier(dims[0])} = {_quote_identifier(dims[1])}'

    else:
        raise ClickHouseQueryTransformException(
            grammar.Name(identifier='UDF'),
            f'Cannot convert Druid filter type "{ftype}" to ClickHouse SQL',
        )
