// Command symdump 提取 Go 包的**包级导出符号表**，供 verify_facade_symbols.py 对账
// 门面仓的别名转发完整性。
//
// ## 为什么用 go/parser 而不是正则或 go doc
//
// 这个工具是门禁的**独立 oracle**：它必须忠实地回答「这个包对外导出了哪些符号」，
// 一旦提取有盲区，门禁就会对着坏门面说「没问题」——那是假通过。
//
// 实测过的两条坑（2026-10-20）：
//
//   - `go doc -all` 会漏符号：块内 const 与部分 type 抓不到，
//     对 textutil 只报出 19 个真实缺失符号，漏了 4 个。
//   - 正则直扫源文件也不可靠：`var ( A, B = 1, 2 )` 这类同名多声明，
//     按行抓只能拿到 A；复合字面量里的缩进字段还可能被误当成声明。
//
// go/parser 解析的是**语法树**，声明的完整形态（块内多名字、泛型参数、
// 接收者）都在节点上，不存在上面两类漏/误。
//
// ## 判定口径
//
//   - 只收**包级**声明：方法（带接收者的 func）不进符号表——
//     门面用 `type T = upstream.T` 做类型别名，方法随类型一起过去，
//     不需要（也不能）单独转发。
//   - 只收**导出**符号（首字母大写）。
//   - 区分「泛型函数」与「泛型类型」：
//     泛型函数无法用变量别名转发（`var F = pkg.F[T]` 不合法），
//     泛型类型可以用泛型类型别名转发（`type G[T any] = upstream.G[T]`，Go 1.24+ 合法）。
//     两者混为一谈会把可转发的泛型类型误报成缺失。
//
// ## 用法
//
//	symdump <包目录>                        # 人看的符号表
//	symdump --manifest <manifest.json>      # 批量模式，读 stdin/写 stdout 的 JSON
//
// 批量模式一次进程处理所有包：门面有 47 个包，逐个起进程的开销不值得，
// 而 CI 的超时是硬约束。
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// Sym 是一个包级导出符号。
type Sym struct {
	Kind    string `json:"kind"`    // type | const | var | func
	Generic bool   `json:"generic"` // 是否带类型参数
}

// UpstreamResult 是某个上游包的提取结果。
type UpstreamResult struct {
	Symbols map[string]Sym `json:"symbols"`
	Error   string         `json:"error,omitempty"`
}

// FacadeResult 是某个门面 alias.go 的解析结果。
type FacadeResult struct {
	Upstream    string            `json:"upstream"`    // upstream import 路径
	Declared    map[string]string `json:"declared"`    // 转发出去的符号 → 转发用的 kind
	LocalDefs   []string          `json:"localDefs"`   // 门面自己定义的（非转发）
	DocGenerics []string          `json:"docGenerics"` // 文档里声明的泛型函数豁免
	Imports     []string          `json:"imports"`     // 全部 import 路径
	Error       string            `json:"error,omitempty"`
}

type manifest struct {
	Upstream []struct {
		Key string `json:"key"`
		Dir string `json:"dir"`
	} `json:"upstream"`
	Facade []struct {
		Key  string `json:"key"`
		File string `json:"file"`
	} `json:"facade"`
}

type response struct {
	Upstream map[string]UpstreamResult `json:"upstream"`
	Facade   map[string]FacadeResult   `json:"facade"`
}

func main() {
	args := os.Args[1:]
	if len(args) == 2 && args[0] == "--manifest" {
		if err := runManifest(args[1]); err != nil {
			fmt.Fprintln(os.Stderr, "symdump:", err)
			os.Exit(1)
		}
		return
	}
	if len(args) != 1 {
		fmt.Fprintln(os.Stderr, "用法: symdump <包目录> | symdump --manifest <manifest.json>")
		os.Exit(2)
	}
	res, err := upstreamSymbols(args[0])
	if err != nil {
		fmt.Fprintln(os.Stderr, "symdump:", err)
		os.Exit(1)
	}
	names := make([]string, 0, len(res))
	for n := range res {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		s := res[n]
		gen := ""
		if s.Generic {
			gen = "\tgeneric"
		}
		fmt.Printf("%s\t%s%s\n", n, s.Kind, gen)
	}
}

func runManifest(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var m manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return fmt.Errorf("manifest 解析失败: %w", err)
	}
	resp := response{
		Upstream: map[string]UpstreamResult{},
		Facade:   map[string]FacadeResult{},
	}
	for _, u := range m.Upstream {
		syms, err := upstreamSymbols(u.Dir)
		r := UpstreamResult{Symbols: syms}
		if err != nil {
			r.Symbols = map[string]Sym{}
			r.Error = err.Error()
		}
		resp.Upstream[u.Key] = r
	}
	for _, f := range m.Facade {
		info, err := facadeInfo(f.File)
		if err != nil {
			info = FacadeResult{Declared: map[string]string{}, Error: err.Error()}
		}
		resp.Facade[f.Key] = info
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(resp)
}

// ---------------------------------------------------------------- 上游包

// upstreamSymbols 解析一个包目录，返回它的包级导出符号表。
func upstreamSymbols(dir string) (map[string]Sym, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("读包目录失败 %s: %w", dir, err)
	}
	fset := token.NewFileSet()
	out := map[string]Sym{}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".go") ||
			strings.HasSuffix(e.Name(), "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, filepath.Join(dir, e.Name()), nil, 0)
		if err != nil {
			// 解析不了 = 判不了。宁可报错也不能当成「这个包没有符号」。
			return nil, fmt.Errorf("解析 %s 失败: %w", e.Name(), err)
		}
		collectUpstream(file, out)
	}
	return out, nil
}

func collectUpstream(file *ast.File, out map[string]Sym) {
	for _, d := range file.Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			// 方法不转发：门面用类型别名带上方法，不需要单独转发。
			if d.Recv != nil || !d.Name.IsExported() {
				continue
			}
			out[d.Name.Name] = Sym{
				Kind:    "func",
				Generic: hasTypeParams(d.Type.TypeParams),
			}
		case *ast.GenDecl:
			for _, s := range d.Specs {
				switch s := s.(type) {
				case *ast.TypeSpec:
					if !s.Name.IsExported() {
						continue
					}
					out[s.Name.Name] = Sym{
						Kind:    "type",
						Generic: hasTypeParams(s.TypeParams),
					}
				case *ast.ValueSpec:
					kind := "var"
					if d.Tok == token.CONST {
						kind = "const"
					}
					for _, n := range s.Names {
						if n.IsExported() {
							out[n.Name] = Sym{Kind: kind}
						}
					}
				}
			}
		}
	}
}

func hasTypeParams(tp *ast.FieldList) bool {
	return tp != nil && len(tp.List) > 0
}

// ---------------------------------------------------------------- 门面 alias.go

// facadeInfo 解析门面的 alias.go：它转发了什么、转发用的 kind 是什么、
// 文档里声明了哪些泛型函数豁免。
func facadeInfo(path string) (FacadeResult, error) {
	res := FacadeResult{Declared: map[string]string{}, LocalDefs: []string{},
		DocGenerics: []string{}, Imports: []string{}}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		return res, fmt.Errorf("解析 %s 失败: %w", path, err)
	}

	for _, imp := range file.Imports {
		p, err := strconv.Unquote(imp.Path.Value)
		if err != nil {
			continue
		}
		res.Imports = append(res.Imports, p)
		// 门面约定：上游用固定别名 upstream 引入。
		if imp.Name != nil && imp.Name.Name == "upstream" {
			res.Upstream = p
		}
	}
	if res.Upstream == "" {
		// 兜底：唯一的一个第三方 import 视作上游。
		for _, p := range res.Imports {
			if strings.HasPrefix(p, "github.com/") {
				res.Upstream = p
				break
			}
		}
	}

	for _, d := range file.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok {
			continue
		}
		for _, s := range gd.Specs {
			switch s := s.(type) {
			case *ast.TypeSpec:
				// 类型别名 = upstream.X 才是转发；`type X struct{...}` 是门面自研定义。
				if s.Assign.IsValid() && referencesUpstream(s.Type) {
					res.Declared[s.Name.Name] = "type"
				} else {
					res.LocalDefs = append(res.LocalDefs, s.Name.Name)
				}
			case *ast.ValueSpec:
				kind := "var"
				if gd.Tok == token.CONST {
					kind = "const"
				}
				forwarded := false
				for _, v := range s.Values {
					if referencesUpstream(v) {
						forwarded = true
						break
					}
				}
				for _, n := range s.Names {
					if forwarded {
						res.Declared[n.Name] = kind
					} else {
						res.LocalDefs = append(res.LocalDefs, n.Name)
					}
				}
			}
		}
	}

	res.DocGenerics = docGenericExemptions(file)
	sort.Strings(res.LocalDefs)
	sort.Strings(res.DocGenerics)
	return res, nil
}

// referencesUpstream 判断表达式里是否引用了 upstream 包（upstream.X）。
func referencesUpstream(expr ast.Expr) bool {
	if expr == nil {
		return false
	}
	found := false
	ast.Inspect(expr, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok && id.Name == "upstream" {
				found = true
				return false
			}
		}
		return true
	})
	return found
}

// docGenericExemptions 从门面文件里解析「泛型函数无法用变量别名转发」那行声明的符号。
//
// 门面的既有写法是：
//
//	// 注意：泛型函数无法用变量别名转发，这些符号请直接引用上游：NewHolder
//
// 这行是**门面对下游的公开承诺**：下游知道该直接引上游。门禁要守住它，
// 否则「文档说有豁免、实际漏了转发」或「文档说有豁免、实际没这个泛型函数了」
// 都会静默漂移。
//
// ⚠️ 扫的是**全文注释**，不是 ast.File.Doc：门面的注释块与 `package` 子句之间
// 隔着一个空行，Go 解析器因此不会把它当成包文档（file.Doc 为 nil）。
// 只认 file.Doc 会让门禁永远报「文档声明是（无）」——一个假红。
func docGenericExemptions(file *ast.File) []string {
	var out []string
	for _, group := range file.Comments {
		for _, line := range strings.Split(group.Text(), "\n") {
			if !strings.Contains(line, "泛型函数") {
				continue
			}
			idx := strings.LastIndex(line, "：")
			if idx < 0 {
				continue
			}
			// ⚠️ LastIndex 返回的是**字节**下标，而全角冒号「：」在 UTF-8 里占 3 字节，
			// 切的时候必须按字节长度跳过整个字符（idx+1 会切掉半个字符，
			// 症状是符号名变成乱码「��GenFn」——这种损坏在门禁里表现为
			// 「文档清单永远对不上」，很难一眼看出是切错了）。
			tail := line[idx+len("："):]
			for _, part := range strings.FieldsFunc(tail, func(r rune) bool {
				return r == '、' || r == ',' || r == '，' || r == ' ' || r == '\t'
			}) {
				part = strings.Trim(part, "。.；;")
				if part != "" {
					out = append(out, part)
				}
			}
		}
	}
	return out
}
