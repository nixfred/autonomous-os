package logger

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"strings"
	"testing"
)

// TestSlogMessagesArePlain keeps user data out of GELF messages: the GELF
// handler redacts attributes by key (gelfSafeKeys) but ships a slog message as
// is, so a message under system/ or runtimes/ must be built from literals and
// named values, never by a formatting call.
func TestSlogMessagesArePlain(t *testing.T) {
	root := filepath.Join("..", "..", "..")
	var leaks []string
	for _, dir := range []string{"system", "runtimes"} {
		err := filepath.WalkDir(filepath.Join(root, dir), func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				if d.Name() == "web" || d.Name() == "node_modules" || d.Name() == "testdata" {
					return filepath.SkipDir
				}
				return nil
			}
			if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
				return nil
			}
			fset := token.NewFileSet()
			file, err := parser.ParseFile(fset, path, nil, 0)
			if err != nil {
				return err
			}
			ast.Inspect(file, func(n ast.Node) bool {
				call, ok := n.(*ast.CallExpr)
				if !ok || !isSlogCall(call) {
					return true
				}
				// The message ships to GELF as is, so it must be built from
				// literals and named values only: no formatting calls.
				if msg := slogMessageArg(call); msg != nil && !isPlainMessage(msg) {
					leaks = append(leaks, fset.Position(msg.Pos()).String()+" message built by a call (put the value in an attribute)")
				}
				return true
			})
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, leak := range leaks {
		t.Errorf("%s", leak)
	}
}

func isSlogCall(call *ast.CallExpr) bool {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok {
		return false
	}
	switch sel.Sel.Name {
	case "Info", "Warn", "Error", "Debug", "InfoContext", "WarnContext", "ErrorContext", "DebugContext", "With":
	default:
		return false
	}
	pkg, ok := sel.X.(*ast.Ident)
	return ok && pkg.Name == "slog"
}

// slogMessageArg returns the message argument of a slog logging call, or nil
// for With.
func slogMessageArg(call *ast.CallExpr) ast.Expr {
	name := call.Fun.(*ast.SelectorExpr).Sel.Name
	idx := 0
	switch {
	case name == "With":
		return nil
	case strings.HasSuffix(name, "Context"):
		idx = 1
	}
	if idx >= len(call.Args) {
		return nil
	}
	return call.Args[idx]
}

func isPlainMessage(expr ast.Expr) bool {
	switch e := expr.(type) {
	case *ast.BasicLit, *ast.Ident:
		return true
	case *ast.ParenExpr:
		return isPlainMessage(e.X)
	case *ast.BinaryExpr:
		return e.Op == token.ADD && isPlainMessage(e.X) && isPlainMessage(e.Y)
	}
	return false
}
