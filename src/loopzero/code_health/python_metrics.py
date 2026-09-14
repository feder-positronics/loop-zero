"""Python span inventory, including nested and asynchronous functions."""

import ast

from .source import digest


def functions(path, text):
    tree = ast.parse(text, filename=path)
    result = []

    def visit(node, parents):
        named = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        names = parents + [node.name] if named else parents
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.append(
                {
                    "path": path,
                    "name": ".".join(names),
                    "line": node.lineno,
                    "end": node.end_lineno,
                    "span": node.end_lineno - node.lineno + 1,
                    "complexity": None,
                    "body": digest(ast.dump(node, include_attributes=False)),
                }
            )
        for child in ast.iter_child_nodes(node):
            visit(child, names)

    visit(tree, [])
    return result
