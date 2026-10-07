"""Legacy numeric-policy utilities used by historical recovery, not publication."""
from arxiv_ra.evidence import attach_evidence as attach_links, TOKEN
from arxiv_ra.models import ParsedPaper
from arxiv_ra.quality import apply_numeric_policy
from arxiv_ra.render import markdown_with_math
from arxiv_ra.source_spans import source_spans
from bs4 import BeautifulSoup
import pytest


def legacy_policy_result(report, parsed, *, pdf_available, full_report):
    from arxiv_ra.quality import audit_report_numbers, preserve_unverified_content, normalized_excerpt
    spans = source_spans(parsed)
    def resolve(match):
        if match[2] is not None:
            return spans.get(match[2].strip())
        quote = match[1]
        hits = [i + 1 for i, page in enumerate(parsed.page_texts)
                if normalized_excerpt(quote) in normalized_excerpt(page)]
        return {'quote': quote, 'page': hits[0]} if len(hits) == 1 else None
    audit = audit_report_numbers(report, TOKEN, lambda quote: any(
        normalized_excerpt(quote) in normalized_excerpt(page) for page in parsed.page_texts),
        resolve_quote=lambda m: (resolve(m) or {}).get('quote'),
        resolve_source=resolve, pages=parsed.page_texts)
    preserve_unverified_content(audit, TOKEN, quiet_tables=True, quiet_prose=True)
    output, evidence = attach_links(apply_numeric_policy(report, audit), parsed,
                                    pdf_available=pdf_available, full_report=full_report)
    return output, {**evidence, 'numeric_audit': audit}


def review(source, headers, rows):
    parsed = ParsedPaper(source, [source])
    key = next(iter(source_spans(parsed)))
    text = "# Test\n\n## 关键结果\n\n| " + " | ".join(headers) + " | 原文依据 |\n"
    text += "|" + "---|" * (len(headers) + 1) + "\n"
    text += "\n".join("| " + " | ".join(row) + f" | [[证据ID:{key}]] |" for row in rows)
    output, evidence = legacy_policy_result(text, parsed, pdf_available=True, full_report=True)
    tree = BeautifulSoup(markdown_with_math(output)[0], "html.parser")
    rendered_rows = [[c.get_text(strip=True) for c in row.select("td")] for row in tree.select("tbody tr")]
    return output, evidence["numeric_audit"], rendered_rows


SOURCE = "| Model | Accuracy (%) | F1 (%) |\n|---|---|---|\n| Alpha | 91.2 | 88.4 |\n| Beta | 88.4 | 91.2 |\n"


def test_short_model_table_separator_is_repaired_before_numeric_audit():
    from arxiv_ra.report import finalize_report_structure
    source = "No source table is available to confirm these extracted values."
    report = "## 关键结果\n\n| Model | Score | 原文依据 |\n| :--- | :--- |\n| **Models** | |\n| Alpha | 99.9 | 缺少依据 |\n| Beta | 88.8 | 缺少依据 |"
    output, evidence = legacy_policy_result(finalize_report_structure(report, []),
                                      ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    tree = BeautifulSoup(markdown_with_math(output)[0], "html.parser")
    assert len(tree.select('tbody tr')) == 3
    assert [[c.get_text(strip=True) for c in row.select('td')] for row in tree.select('tbody tr')] == [
        ['Models', '', ''], ['Alpha', '99.9', '缺少依据'], ['Beta', '88.8', '缺少依据']]
    assert '**[待核对]** |' not in output
    assert evidence['numeric_audit']['table_diagnostics']


def test_column_and_model_swaps_are_caught_even_when_all_numbers_exist():
    _, audit, rows = review(SOURCE, ["Model", "Accuracy (%)", "F1 (%)"],
                            [["Alpha", "88.4", "91.2"], ["Beta", "91.2", "88.4"]])
    assert len(audit["issues"]) == 2
    assert audit["publication"]["withheld_cells"] == 0
    assert audit["publication"]["flagged_cells"] == 4
    assert [r[:3] for r in rows] == [["Alpha", "88.4（引用冲突）", "91.2（引用冲突）"], ["Beta", "91.2（引用冲突）", "88.4（引用冲突）"]]
    assert all(i["reason"] == "column_value_mismatch" for i in audit["issues"])
    assert "88.4" in audit["issues"][0]["original"]


def test_correct_reordered_columns_are_bound_by_header_not_position():
    _, audit, rows = review(SOURCE, ["Model", "F1 (%)", "Accuracy (%)"], [["Alpha", "88.4", "91.2"]])
    assert not audit["issues"] and rows[0][:3] == ["Alpha", "88.4", "91.2"]
    assert audit["table_checks"][0]["status"] == "headers_checked"


def test_explicit_source_column_unit_supports_percent_cell_without_conversion():
    _, audit, rows = review(SOURCE, ["Model", "Accuracy (%)", "F1 (%)"], [["Alpha", "91.2%", "88.4"]])
    assert not audit["issues"] and rows[0][1] == "91.2%"
    _, audit, rows = review(SOURCE, ["Model", "Accuracy (%)", "F1 (%)"], [["Alpha", "0.912", "88.4"]])
    assert audit["issues"] and rows[0][1] == "0.912（引用冲突）"


def test_header_unit_spacing_and_arrow_placement_are_formatting_only():
    source = SOURCE.replace("Accuracy (%)", "Accuracy ↑ (%)")
    _, audit, rows = review(source, ["Model", "Accuracy (% ) ↑", "F1 (%)"], [["Alpha", "91.2", "88.4"]])
    assert not audit["issues"] and rows[0][1] == "91.2"


@pytest.mark.parametrize('arrow', [r'\(\uparrow\)', r'$\uparrow$'])
def test_live_model_latex_header_arrow_is_equivalent_to_source_unicode(arrow):
    source = 'Model\nCLIP (↑)\nMSE (↓)\nAlpha\n0.9634\n10488\n'
    _, audit, rows = review(source, ['Model', f'CLIP ({arrow})', r'MSE (\(\downarrow\))'],
                            [['Alpha', '0.9634', '10488']])
    assert not audit['issues'] and rows[0][1:3] == ['0.9634', '10488']
    assert audit['table_checks'][0]['status'] == 'headers_checked'
    _, audit, rows = review(source, ['Model', r'CLIP (\(\downarrow\))', r'MSE (\(\downarrow\))'],
                            [['Alpha', '0.9634', '10488']])
    assert audit['issues'] and rows[0][1] == '0.9634（引用冲突）' and rows[0][2] == '10488'


@pytest.mark.parametrize("header", ["Accuracy (ms)", "Accuracy (10^-2)", "Accuracy"])
def test_header_units_and_scaling_cannot_be_silently_changed(header):
    _, audit, rows = review(SOURCE, ["Model", header, "F1 (%)"], [["Alpha", "91.2", "88.4"]])
    assert rows[0][:3] == ["Alpha", "91.2（引用冲突）", "88.4"]
    assert audit["publication"]["withheld_cells"] == 0
    assert audit["publication"]["flagged_cells"] == 1


def test_explicit_dataset_and_split_conditions_do_not_cross_bind():
    source = "| Model | Dataset | Split | Accuracy |\n|---|---|---|---|\n| Alpha | A | test | 91.2 |\n"
    _, audit, rows = review(source, ["Model", "Dataset", "Split", "Accuracy"], [["Alpha", "B", "test", "91.2"]])
    assert rows[0][3] == "91.2（引用冲突）" and audit["issues"][0]["reason"] == "row_context_mismatch"


PDF = "Table 3: Editing results\nMethod\nACC\nrMSE\nRLD\nMSE\nEasy\nAlpha [12]†\n62.63\n75.16\n1.45\n5.63\nMedium\nAlpha [12]†\n46.00\n53.11\n140.68\n8.14\n"


def test_pdf_row_projection_preserves_omitted_metric_and_reports_weaker_check():
    _, audit, rows = review(PDF, ["Difficulty", "Model", "ACC", "MSE"], [["Easy", "Alpha", "62.63", "5.63"]])
    assert not audit["issues"] and rows[0][2:4] == ["62.63", "5.63"]
    assert audit["table_checks"][0]["status"] == "row_order_checked"
    assert audit["table_checks"][0]["reason"] == "headers_not_assessed"


def test_flat_pdf_headers_bind_metrics_and_allow_deliberate_projection():
    source = "Model\nAccuracy (%)\nF1 (%)\nAlpha\n91.2\n88.4\nBeta\n88.4\n91.2\n"
    _, audit, rows = review(source, ["Model", "F1 (%)", "Accuracy (%)"], [["Beta", "91.2%", "88.4"]])
    assert not audit["issues"] and rows[0][1] == "91.2%"
    assert audit["table_checks"][0]["status"] == "headers_checked"
    _, audit, rows = review(source, ["Model", "Accuracy (%)", "F1 (%)"], [["Alpha", "88.4", "91.2"]])
    assert rows[0][1:3] == ["88.4（引用冲突）", "91.2（引用冲突）"]
    assert audit["issues"][0]["reason"] == "column_value_mismatch"


def test_pdf_row_permutation_and_wrong_difficulty_values_are_not_accepted():
    _, audit, rows = review(PDF, ["Difficulty", "Model", "ACC", "MSE"],
                            [["Easy", "Alpha", "5.63", "62.63"], ["Medium", "Alpha", "62.63", "5.63"]])
    assert len(audit["issues"]) == 2 and all('引用冲突' in r[2] and '引用冲突' in r[3] for r in rows)
    assert rows[0][2:4] == ['5.63（引用冲突）', '62.63（引用冲突）']
    assert rows[1][2:4] == ['62.63（引用冲突）', '5.63（引用冲突）']


def test_missing_row_condition_and_damaged_numbers_are_not_guessed():
    _, audit, _ = review(PDF, ["Model", "ACC", "MSE"], [["Alpha", "62.63", "5.63"]])
    assert audit["table_checks"][0]["status"] == "unassessed"
    damaged = "Table 1: Results\nAlpha\n1.22\n33.90\n1656.5010.20\n34.87\n"
    _, audit, rows = review(damaged, ["Model", "ACC", "MSE", "CCR"], [["Alpha", "1.22", "10.20", "34.87"]])
    assert audit["table_checks"][0]["status"] == "unassessed"
    assert rows[0][:4] == ["Alpha", "1.22", "10.20", "34.87"]
    assert audit['table_diagnostics'] and not audit['issues']


@pytest.mark.parametrize('scores', [['14.55', '75.93'], ['14.55']])
def test_pdf_medal_and_mixed_type_cells_cannot_establish_a_conflict(scores):
    # EdiVal Table 3: a medal glyph becomes "5", then a checkbox/date
    # interrupts the parser before it reaches any actual metric cells.
    source = 'Table 3: Results\nModel\nLatency\nScore\n∗Seedream 4.0 5\n✗\n25.09.10\n14.55\n75.93\n'
    output, audit, rows = review(source, ['Model', 'Latency', 'Score'][:len(scores) + 1],
                                [['∗Seedream 4.0', *scores]])
    assert '引用冲突' not in output
    assert audit['table_checks'][0]['status'] == 'unassessed'
    assert rows[0][1:len(scores) + 1] == scores


def test_partial_numeric_source_row_is_not_evidence_of_wrong_values():
    source = 'Table 3: Results\nAlpha\n5\nunsupported categorical cell\n14.55\n75.93\n'
    output, audit, _ = review(source, ['Model', 'Latency', 'Score'], [['Alpha', '14.55', '75.93']])
    assert '引用冲突' not in output
    assert audit['table_checks'][0]['status'] == 'unassessed'


def test_plain_numeric_prefix_cannot_bind_mixed_categorical_columns():
    source = 'Table 3: Results\nModel\nRank\nIn-Context\nScore\nAlpha 5\nYes\n75.93\n'
    output, audit, _ = review(source, ['Model', 'In-Context', 'Score'], [['Alpha', 'Yes', '75.93']])
    assert '引用冲突' not in output
    assert audit['table_checks'][0]['reason'] == 'unsupported_mixed_type_row'
    # Explicit matrices still prove a real numeric conflict in this layout.
    source = '| Model | In-Context | Score |\n|---|---|---|\n| Alpha | Yes | 75.93 |'
    _, audit, rows = review(source, ['Model', 'In-Context', 'Score'], [['Alpha', 'Yes', '99.9']])
    assert rows[0][2] == '99.9（引用冲突）'


def test_bad_cell_does_not_remove_correct_siblings_or_break_escaped_pipes():
    source = "The Alpha model achieved 91.2% accuracy on the test set."
    _, audit, rows = review(source, ["Model", "Accuracy", "Unknown", "Note"], [["Alpha", "91.2%", "99.9%", r"a\|b"]])
    assert rows[0][:4] == ["Alpha", "91.2%", "99.9%", "a|b"]
    assert audit["publication"]["flagged_cells"] == 0
    assert audit['table_diagnostics'][0]['numbers'] == ['99.9%']


def test_optional_outer_pipes_preserve_table_and_good_cells():
    source = "The Alpha model achieved 91.2% accuracy on the test set."
    report = f"## 关键结果\n\nModel | Score | Other | 依据\n---|---|---|---\nAlpha | 91.2% | 99.9% | [[证据:{source}]]"
    output, evidence = legacy_policy_result(report, ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    tree = BeautifulSoup(markdown_with_math(output)[0], "html.parser")
    assert [c.get_text(strip=True) for c in tree.select("tbody td")][:3] == ["Alpha", "91.2%", "99.9%"]
    assert evidence["numeric_audit"]["publication"]["flagged_cells"] == 0
    assert evidence['numeric_audit']['table_diagnostics']


def test_unverified_prose_keeps_content_and_separate_diagnostics():
    source = "The experimental outcome needs additional study before conclusions."
    report = "# Test\n\n## 关键结果\n\n### A\n- Alpha: 91.2%\n- Beta: 99.9%\n\n原有定性描述。\n\n### B\n可靠文字。"
    output, evidence = legacy_policy_result(report, ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    assert "91.2" in output and "99.9" in output
    assert output.count("**[待核对]**") == 0
    assert "原有定性描述。" in output and "### B\n可靠文字。" in output
    assert evidence["numeric_audit"]["publication"]["flagged_claims"] == 0
    assert [i["original"] for i in evidence["numeric_audit"]["prose_diagnostics"]] == ["- Alpha: 91.2%", "- Beta: 99.9%"]
    with pytest.raises(ValueError):
        apply_numeric_policy("changed", {'issues': evidence['numeric_audit']['prose_diagnostics']})


def test_fenced_examples_are_not_publication_targets():
    source = "The source contains no quantitative results in this example."
    report = "## 关键结果\n\n```md\n## 实验设置\n| Model | Score |\n|---|---|\n| A | 99.9 |\n```\n\n定性说明。"
    output, evidence = legacy_policy_result(report, ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    assert not evidence["numeric_audit"]["issues"] and "99.9" in output


def test_display_math_with_minus_lines_is_not_treated_as_a_numeric_list():
    source = "This source explains the symbolic loss definition for the task."
    formula = "\\[\nx = y\n- 1 + z\n\\]"
    report = "## 关键结果\n\n" + formula + "\n\n数值结论 99.9%。"
    output, evidence = legacy_policy_result(report, ParsedPaper(source, [source]), pdf_available=True, full_report=True)
    assert formula in output and "数值结论 99.9%" in output
    assert '**[待核对]**' not in output
    assert len(evidence["numeric_audit"]["prose_diagnostics"]) == 1


def test_same_page_unrelated_quote_cannot_validate_a_different_row():
    parsed = ParsedPaper("", [SOURCE + "\n" + "Unrelated narrative. " * 40])
    key = list(source_spans(parsed))[-1]
    report = f"## 关键结果\n\n| Model | Accuracy (%) |\n|---|---|\n| Alpha | 91.2 | [[证据ID:{key}]] |"
    # Keep the source column present, so the Markdown itself remains valid.
    report = report.replace("| Model | Accuracy (%) |", "| Model | Accuracy (%) | 依据 |").replace("|---|---|", "|---|---|---|")
    _, evidence = legacy_policy_result(report, parsed, pdf_available=True, full_report=True)
    assert evidence["numeric_audit"]["table_checks"][0]["status"] == "unassessed"
    assert evidence["numeric_audit"]["table_diagnostics"][0]["numbers"] == ["91.2"]
