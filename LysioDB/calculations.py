import pandas as pd
import polars as pl
from ipfn import ipfn
from typing import Dict, List, Any, Optional


class Calculations:
    def __init__(self, database):
        """
        Initialize the class with a dataframe and optional metadata.
        """
        self.database = database

        print("Initialization of Calculations object complete.")

    def weights_test(
        self, file_name: str, target_columns: list, df_columns: list
    ) -> pl.DataFrame:
        """
        Vectorized IPF using map_batches for better performance.
        """
        print("\n--- Starting Polars IPF Weight Calculation ---")

        # 1. Load and prepare target data
        target_df = (
            pl.read_excel(file_name)
            .melt(id_vars=["Område", "Ålder"], value_vars=["Kvinna", "Man"])
            .rename({"variable": "Kön", "value": "population"})
        )

        # 2. Prepare survey data with mapped columns
        survey_df = self.database.df.with_columns(
            [pl.col(col).alias(f"{col}_mapped") for col in df_columns]
        )

        # 3. Initialize weights
        weight_df = survey_df.with_columns(pl.lit(1.0).alias("weight"))

        # 4. Pre-compute target marginals
        target_marginals = {
            col: target_df.lazy()
            .group_by(target_col)
            .agg(pl.col("population").sum())
            .collect()
            .to_dict(as_series=False)
            for col, target_col in zip(df_columns, target_columns)
        }

        # 5. IPF Iteration
        max_iterations = 1000
        tolerance = 1e-6
        converged = False

        for iteration in range(max_iterations):
            prev_weights = weight_df["weight"].clone()

            for col, target_col in zip(df_columns, target_columns):
                current_marginals = (
                    weight_df.lazy()
                    .group_by(f"{col}_mapped")
                    .agg(pl.col("weight").sum())
                    .drop_nans()
                    .collect()
                    .to_dict(as_series=False)
                )
                adj_df = pl.DataFrame(
                    {
                        f"{col}_mapped": current_marginals[f"{col}_mapped"],
                        "weight": current_marginals["weight"],
                        "target": target_marginals[col]["population"],
                    }
                ).with_columns(
                    (pl.col("target") / pl.col("weight"))
                    .fill_nan(1.0)
                    .fill_null(1.0)
                    .alias("factor")
                )

                weight_df = (
                    weight_df.lazy()
                    .join(
                        adj_df.lazy().select([f"{col}_mapped", "factor"]),
                        on=f"{col}_mapped",
                        how="left",
                    )
                    .with_columns(
                        (pl.col("weight") * pl.col("factor").fill_null(1.0)).alias(
                            "weight"
                        )
                    )
                    .drop("factor")
                    .collect()
                )

            weight_diff = (weight_df["weight"] - prev_weights).abs().max()
            if weight_diff < tolerance:
                converged = True
                break

        print(
            f"\nConverged after {iteration + 1} iterations"
            if converged
            else f"Max iterations reached (diff: {weight_diff})"
        )
        self.database.df = self.database.df.with_columns(
            weight_df["weight"].alias("weight")
        )

        return self.database.df

    def weights(self, file_name, target_columns: list, df_columns: list):
        """
        Dynamically calculates weights for a survey dataset using Iterative Proportional Fitting (IPF).


        Args:

            file_name (str): Path to the Excel file containing population targets.

            target_columns (list): Column names in the target data (Excel).

            df_columns (list): Corresponding column names in the survey data (`self.df`).


        Returns:

            pd.DataFrame: Survey data with calculated weights.

        """

        print("\n--- Start calculating weights ---")

        target_df = pd.read_excel(file_name)
        target_df = target_df.melt(
            id_vars=["Område", "Ålder"],
            value_vars=["Kvinna", "Man"],
            var_name="Kön",
            value_name="population",
        )
        df = self.database.df.to_pandas()

        mapped_cols = {}

        for col in df_columns:
            if col in self.database.meta.variable_value_labels:
                mapped_col_name = f"{col}_mapped"

                mapped_cols[mapped_col_name] = (
                    df[col]
                    .map(self.database.metadata.get_value_labels(column=col))
                    .fillna(df[col])
                )

        if mapped_cols:
            for col, values in mapped_cols.items():
                df[col] = values

        target_marginals = (
            target_df.groupby(target_columns)["population"].sum().to_dict()
        )
        weight_matrix = (
            df.groupby([f"{col}_mapped" for col in df_columns])
            .size()
            .reset_index(name="total")
        )

        all_combinations = pd.MultiIndex.from_product(
            [list(target_df[col].unique()) for col in target_columns],
            names=[f"{col}_mapped" for col in df_columns],
        )

        weight_matrix = (
            weight_matrix.set_index([f"{col}_mapped" for col in df_columns])
            .reindex(all_combinations, fill_value=0)
            .reset_index()
        )

        aggregates = [
            pd.Series(
                {key[i]: value for key, value in target_marginals.items()}
            ).reindex(weight_matrix[col + "_mapped"].unique(), fill_value=0)
            for i, col in enumerate(df_columns)
        ]

        dimensions = [[f"{df_columns[i]}_mapped"] for i in range(len(df_columns))]

        print("\nRunning IPFN...")

        ipfn_instance = ipfn.ipfn(weight_matrix, aggregates, dimensions)

        weight_matrix["weight"] = ipfn_instance.iteration().iloc[:, -1]

        weight_matrix["weight"] = weight_matrix["weight"].fillna(1)

        df = df.merge(
            weight_matrix[[f"{col}_mapped" for col in df_columns] + ["weight"]],
            left_on=[f"{col}_mapped" for col in df_columns],
            right_on=[f"{col}_mapped" for col in df_columns],
            how="left",
        )
        df["weight"] = df["weight"].fillna(1)

        mapped_columns = [col for col in df.columns if col.endswith("_mapped")]

        df.drop(columns=mapped_columns, inplace=True)

        self.database.df = pl.DataFrame(df)

        print("\n--- done with calculations ---")

        return self.database.df

    def percentages(self, weights=False):
        """
        Calculate percentages for different types of questions using vectorized Polars operations
        within a single function.
        """
        print("\n--- Start calculating percentages ---")

        use_weights = weights and (self.database.config.WEIGHT_COLUMN is not None)
        if weights and not use_weights:
            print(
                "Warning: Weighting requested but weight column not available. Calculating unweighted percentages."
            )

        category_cols = self.database.categories.to_list()
        question_cols = self.database.question_df["question"].to_list()

        cols_to_select = category_cols + question_cols
        if use_weights:
            cols_to_select.append(self.database.config.WEIGHT_COLUMN)

        # df_calc = self.database.df.select(cols_to_select, strict=False)
        df_calc = self.database.df.clone()

        nan_values_config = self.database.config.NAN_VALUES

        nan_values_list = []
        if isinstance(nan_values_config, (set, dict)):
            nan_values_list = list(
                nan_values_config.keys()
                if isinstance(nan_values_config, dict)
                else nan_values_config
            )
            if not nan_values_list:
                print(
                    "Warning: config.NAN_VALUES is empty. No specific NaN values to replace or count."
                )
        else:
            print(
                f"Warning: config.NAN_VALUES is not a set or dict ({type(nan_values_config)}). Cannot replace or count specific NaN values."
            )

        percentage_results_list: List[pl.DataFrame] = []

        question_groups = (
            self.database.question_df.group_by(["base_question", "question_type"])
            .agg(
                [
                    pl.col("question").unique().alias("columns"),
                    pl.col("value_labels_info").first().alias("value_labels_info"),
                    pl.col("question_label").alias("question_labels"),
                    pl.col("base_question_label").first().alias("base_question_label"),
                ]
            )
            .iter_rows(named=True)
        )

        for group in question_groups:
            base_question = group["base_question"]
            question_type = group["question_type"]
            columns = group["columns"]
            value_labels_info = group["value_labels_info"]
            # question_labels = group["question_labels"]
            # base_question_label = group["base_question_label"]

            cols_for_this_group = columns + category_cols
            if use_weights:
                cols_for_this_group.append(self.database.config.WEIGHT_COLUMN)

            question_map = self.database.config.question_map

            if question_map and base_question in question_map:
                condition_polars_str = question_map[base_question]
                evaluated_expr = eval(condition_polars_str, {"pl": pl, "df": df_calc})
                df_filtered = df_calc.filter(evaluated_expr)
                df_group = df_filtered.select(cols_for_this_group, strict=False)
            else:
                df_group = df_calc.select(cols_for_this_group, strict=False)

            if question_type in ["multi_response", "grid", "single_choice"]:
                if value_labels_info:
                    possible_values = list(value_labels_info.keys())
                    if nan_values_list:
                        possible_values = [
                            val
                            for val in possible_values
                            if float(val) not in nan_values_list
                        ]
                        if not possible_values:
                            print(
                                f"Warning: All values for base question '{base_question}' are in NAN_VALUES. Skipping percentage calculation for specific values."
                            )
                else:
                    print(
                        f"Warning: No value labels found for base question '{base_question}'. Cannot calculate percentages based on values. Skipping."
                    )
                    continue

                if not possible_values:
                    print(
                        f"Warning: No possible values found for base question '{base_question}'. Skipping percentage calculation."
                    )
                    continue

                aggregation_expressions = []

                for col in columns:
                    if col in df_group.columns:
                        col_dtype = df_group[col].dtype

                        for value in possible_values:
                            try:
                                literal_value_expr = pl.lit(value).cast(col_dtype)

                                count_expr = (
                                    (pl.col(col) == literal_value_expr)
                                    .sum()
                                    .cast(pl.Float64)
                                    .alias(f"{col}_{value}_count")
                                )
                                aggregation_expressions.append(count_expr)

                                if use_weights:
                                    weighted_expr = (
                                        pl.when(pl.col(col) == literal_value_expr)
                                        .then(
                                            pl.col(self.database.config.WEIGHT_COLUMN)
                                        )
                                        .sum()
                                        .cast(pl.Float64)
                                        .alias(f"{col}_{value}_weighted")
                                    )
                                    aggregation_expressions.append(weighted_expr)
                            except pl.exceptions.ComputeError as e:
                                print(
                                    f"Warning: Could not cast value '{value}' to dtype {col_dtype} for column '{col}'. Skipping aggregation for this value: {e}"
                                )
                                continue
                            except Exception as e:
                                print(
                                    f"Warning: An unexpected error occurred while processing value '{value}' for column '{col}': {e}"
                                )
                                continue

                        if nan_values_list:
                            nan_count_expr = (
                                pl.col(col)
                                .is_in(nan_values_list)
                                .sum()
                                .cast(pl.Float64)
                                .alias(f"{col}_nan_count")
                            )
                            aggregation_expressions.append(nan_count_expr)

                            if use_weights:
                                nan_weighted_expr = (
                                    pl.when(pl.col(col).is_in(nan_values_list))
                                    .then(pl.col(self.database.config.WEIGHT_COLUMN))
                                    .sum()
                                    .cast(pl.Float64)
                                    .alias(f"{col}_nan_weighted")
                                )
                                aggregation_expressions.append(nan_weighted_expr)
                        else:
                            aggregation_expressions.append(
                                pl.lit(0.0).alias(f"{col}_nan_count")
                            )
                            if use_weights:
                                aggregation_expressions.append(
                                    pl.lit(0.0).alias(f"{col}_nan_weighted")
                                )

                        total_count_for_col_expr = (
                            pl.col(col)
                            .filter(~pl.col(col).is_in(nan_values_list))
                            .count()
                            .cast(pl.Float64)
                            .alias(f"{col}_total_count")
                        )
                        aggregation_expressions.append(total_count_for_col_expr)
                        if use_weights:
                            total_weighted_count_for_col_expr = (
                                pl.when(~pl.col(col).is_in(nan_values_list))
                                .then(pl.col(self.database.config.WEIGHT_COLUMN))
                                .sum()
                                .cast(pl.Float64)
                                .alias(f"{col}_total_weighted_count")
                            )
                            aggregation_expressions.append(
                                total_weighted_count_for_col_expr
                            )

                if not aggregation_expressions:
                    print(
                        f"Warning: No aggregation expressions created for base question '{base_question}'. Skipping."
                    )
                    continue

                results_for_this_group_list = []

                for category_col in category_cols:
                    if category_col not in df_group.columns:
                        print(
                            f"Warning: Category column '{category_col}' not found in DataFrame for base question '{base_question}'. Skipping aggregation for this category."
                        )
                        continue

                    grouped_agg_df = df_group.group_by(pl.col(category_col)).agg(
                        aggregation_expressions
                    )

                    grouped_agg_df = grouped_agg_df.filter(
                        pl.col(category_col).is_not_null()
                    )
                    grouped_agg_df = grouped_agg_df.rename({category_col: "Category"})
                    grouped_agg_df = grouped_agg_df.with_columns(
                        pl.lit(category_col).alias("Category")
                    )
                    percentage_expressions = []

                    for col in columns:
                        if col in grouped_agg_df.columns or any(
                            c.startswith(f"{col}_") for c in grouped_agg_df.columns
                        ):
                            for value in possible_values:
                                count_col_name = f"{col}_{value}_count"
                                percentage_col_name = f"{col}_{value}_percentage"
                                total_count = f"{col}_total_count"
                                if count_col_name in grouped_agg_df.columns:
                                    if use_weights:
                                        weighted = f"{col}_{value}_weighted"
                                        total_weighted = f"{col}_total_weighted_count"
                                        if weighted in grouped_agg_df.columns:
                                            percentage_expressions.append(
                                                (
                                                    pl.col(weighted)
                                                    / pl.col(total_weighted)
                                                )
                                                .fill_null(0)
                                                .cast(pl.Float64)
                                                .alias(percentage_col_name)
                                            )
                                        else:
                                            print(
                                                f"Warning: Weighted sum column '{weighted}' not found for base question '{base_question}', column '{col}', value '{value}', category '{category_col}'. Skipping percentage."
                                            )
                                    else:
                                        percentage_expressions.append(
                                            (
                                                pl.col(count_col_name)
                                                / pl.col(total_count)
                                            )
                                            .fill_null(0)
                                            .fill_nan(0)
                                            .cast(pl.Float64)
                                            .alias(percentage_col_name)
                                        )
                                else:
                                    print(
                                        f"Warning: Count column '{count_col_name}' not found for base question '{base_question}', column '{col}', value '{value}', category '{category_col}'. Skipping percentage."
                                    )

                            nan_count_col_name = f"{col}_nan_count"
                            nan_percentage_col_name = f"{col}_nan_percentage"
                            if nan_count_col_name in grouped_agg_df.columns:
                                if use_weights:
                                    nan_weighted_col_name = f"{col}_nan_weighted"
                                    total_count = f"{col}_total_count"
                                    if nan_weighted_col_name in grouped_agg_df.columns:
                                        percentage_expressions.append(
                                            (
                                                pl.col(nan_weighted_col_name)
                                                / (
                                                    pl.col(total_count)
                                                    + pl.col(nan_weighted_col_name)
                                                )
                                            )
                                            .fill_null(0)
                                            .fill_nan(0)
                                            .cast(pl.Float64)
                                            .alias(nan_percentage_col_name)
                                        )
                                    else:
                                        print(
                                            f"Warning: Weighted nan sum column '{nan_weighted_col_name}' not found for base question '{base_question}', column '{col}', category '{category_col}'. Skipping percentage."
                                        )
                                else:
                                    percentage_expressions.append(
                                        (
                                            pl.col(nan_count_col_name)
                                            / (
                                                pl.col(total_count)
                                                + pl.col(nan_count_col_name)
                                            )
                                        )
                                        .fill_null(0)
                                        .fill_nan(0)
                                        .cast(pl.Float64)
                                        .alias(nan_percentage_col_name)
                                    )
                            else:
                                print(
                                    f"Warning: NaN count column '{nan_count_col_name}' not found for base question '{base_question}', column '{col}', category '{category_col}'. Skipping percentage."
                                )

                    if percentage_expressions:
                        grouped_agg_df = grouped_agg_df.with_columns(
                            percentage_expressions
                        )

                        results_for_this_group_list.append(grouped_agg_df)

                    else:
                        print(
                            f"Warning: No percentage expressions created for base question '{base_question}' and category '{category_col}'. Skipping."
                        )

                if results_for_this_group_list:
                    percentage_results_list.append(
                        pl.concat(results_for_this_group_list, how="vertical")
                    )
                else:
                    print(
                        f"No percentage results generated for base question '{base_question}'."
                    )

            elif question_type == "ranking":
                ranking_results = self._calculate_ranking_metrics(
                    df_group,
                    base_question,
                    columns,
                    value_labels_info,
                    use_weights,
                    category_cols,
                )
                self.database.ranked_df = pl.concat(ranking_results, how="diagonal")

            elif question_type in ["open_text", "numeric_other", "unknown"]:
                pass

        final_results_df = None
        if percentage_results_list:
            combined_df = pl.concat(percentage_results_list, how="diagonal")
            # combined_df = combined_df.select(pl.exclude("^.*_weighted$"))

            unpivot_index_vars = [
                "Category",
            ]
            metric_suffixes = (
                "_count",
                "_percentage",
                "_weighted",
                "_total_count",
                "_nan_count",
                "_nan_percentage",
                "_nan_weighted",
            )
            unpivot_on_vars = [
                col
                for col in combined_df.columns
                if col.endswith(metric_suffixes) or col == "total_denominator_denom"
            ]

            temp_long_df = combined_df.unpivot(
                index=unpivot_index_vars,
                on=unpivot_on_vars,
                variable_name="aggregated_metric",
                value_name="value",
            )
            temp_long_df = temp_long_df.with_columns(pl.col("value").cast(pl.Float64))
            temp_long_df = temp_long_df.drop_nulls(subset=["value"])

            split_cols = pl.col("aggregated_metric").str.split("_")

            temp_long_df = temp_long_df.with_columns(
                [
                    split_cols.list.get(-1).alias("metric_type_raw"),
                    split_cols.list.get(-2).alias("answer_value_raw"),
                    split_cols.list.head(split_cols.list.len() - 2)
                    .list.join("_")
                    .alias("original_column_raw"),
                ]
            )

            temp_long_df = (
                temp_long_df.with_columns(
                    [
                        pl.when(
                            pl.col("metric_type_raw").is_in(
                                [
                                    "count",
                                    "percentage",
                                    "weighted",
                                ]
                            )
                        )
                        .then(pl.col("metric_type_raw"))
                        .when(pl.col("aggregated_metric") == "total_denominator_denom")
                        .then(pl.lit("total_denominator"))
                        .when(
                            (pl.col("metric_type_raw") == "count")
                            & (
                                pl.col("aggregated_metric").str.ends_with(
                                    "_total_count"
                                )
                            )
                        )
                        .then(pl.lit("total_count"))
                        .when(pl.col("metric_type_raw") == "nan_count")
                        .then(pl.lit("nan_count"))
                        .when(pl.col("metric_type_raw") == "nan_percentage")
                        .then(pl.lit("nan_percentage"))
                        .when(pl.col("metric_type_raw") == "nan_weighted")
                        .then(pl.lit("nan_weighted"))
                        .otherwise(pl.lit(None, dtype=pl.Utf8))
                        .alias("metric_type"),
                    ]
                )
                .with_columns(
                    [
                        pl.when(
                            pl.col("metric_type").is_in(
                                [
                                    "count",
                                    "percentage",
                                    "weighted",
                                    "total_count",
                                    "nan_count",
                                    "nan_percentage",
                                    "nan_weighted",
                                ]
                            )
                        )
                        .then(pl.col("original_column_raw"))
                        .otherwise(pl.lit(None, dtype=pl.Utf8))
                        .alias("question"),
                        pl.when(
                            pl.col("metric_type").is_in(
                                [
                                    "count",
                                    "percentage",
                                    "weighted",
                                ]
                            )
                        )
                        .then(pl.col("answer_value_raw"))
                        .when(
                            pl.col("metric_type").is_in(
                                [
                                    "total_count",
                                    "total_denominator",
                                    "nan_count",
                                    "nan_percentage",
                                    "nan_weighted",
                                ]
                            )
                        )
                        .then(pl.lit(None, dtype=pl.Utf8))
                        .otherwise(pl.lit(None, dtype=pl.Utf8))
                        .alias("answer_value"),
                    ]
                )
                .drop(["metric_type_raw", "answer_value_raw", "original_column_raw"])
            )

            pivot_index_cols = [
                "question",
                "answer_value",
                "metric_type",
            ]

            pivot_column = "Category"

            pivot_value = "value"

            final_results_df = temp_long_df.pivot(
                index=pivot_index_cols,
                columns=pivot_column,
                values=pivot_value,
                aggregate_function="first",
            )

            question_order_df = self.database.question_df.select(
                ["question", "base_question_label", "question_label", "question_type"]
            )
            final_result_ordered_df = question_order_df.join(
                final_results_df, on="question", how="left"
            )

            question_value_to_label_map = {}

            for q_id, labels_map in self.database.meta.variable_value_labels.items():
                question_type = self.database.question_df.filter(
                    pl.col("question") == q_id
                ).select("question_type")
                if question_type.is_empty():
                    continue

                if question_type.item() == "multi_response":
                    if (q_id, str(1)) not in question_value_to_label_map:
                        question_value_to_label_map[(q_id, str(0.0))] = "Not selected"
                        question_value_to_label_map[(q_id, str(1.0))] = (
                            self.database.question_df.filter(pl.col("question") == q_id)
                            .select("question_label")
                            .item()
                        )

                else:
                    for val, label in labels_map.items():
                        if val in self.database.config.NAN_VALUES.keys():
                            question_value_to_label_map[(q_id, "nan")] = label
                        else:
                            question_value_to_label_map[(q_id, str(val))] = label

            final_result_ordered_df = (
                final_result_ordered_df.with_columns(
                    [
                        pl.when(pl.col("question_type") == "multi_response")
                        .then(
                            pl.when(pl.col("answer_value") == "0.0")
                            .then(pl.lit("Not select"))
                            .when(pl.col("answer_value") == "1.0")
                            .then(pl.col("question_label"))
                            .otherwise(
                                pl.struct(["question", "answer_value"]).map_elements(
                                    lambda s: question_value_to_label_map.get(
                                        (s.get("question"), s.get("answer_value")), None
                                    ),
                                    return_dtype=pl.Utf8,
                                )
                            )
                        )
                        .otherwise(
                            pl.struct(["question", "answer_value"]).map_elements(
                                lambda s: question_value_to_label_map.get(
                                    (s.get("question"), s.get("answer_value")), None
                                ),
                                return_dtype=pl.Utf8,
                            )
                        )
                        .alias("answer_label"),
                        pl.when(pl.col("question_type") == "multi_response")
                        .then(pl.col("base_question_label"))
                        .otherwise(pl.col("question_label"))
                        .alias("display_question_label"),
                    ]
                )
                .filter(
                    (
                        (
                            (pl.col("answer_label").is_not_null())
                            | (pl.col("answer_value") == "total")
                        )
                        & (pl.col("answer_value") != "0.0")
                    )
                )
                .fill_null("Total")
                .drop(
                    [
                        "question_type",
                        "base_question_label",
                        "question_label",
                        # "answer_value",
                    ]
                )
            )

            first_columns = [
                "question",
                "display_question_label",
                "answer_label",
                "answer_value",
            ]
            final_result_ordered_df = final_result_ordered_df.select(
                first_columns
                + [
                    col
                    for col in final_result_ordered_df.columns
                    if col not in first_columns
                ]
            )

            print(
                "Answer values replaced with labels and unlabelled non-total rows filtered."
            )

        else:
            final_results_df = pl.DataFrame()
            print("Step 5: No percentage results to combine or pivot.")

        print(
            "\n--- Percentage calculations complete. Returning DataFrame in pivoted format. ---"
        )

        self.database.percentage_df = final_result_ordered_df

        return final_results_df

    def _calculate_ranking_metrics(
        self,
        df_group_calc: pl.DataFrame,
        base_question: str,
        columns: List[str],
        value_labels_info: Optional[Dict[Any, str]],
        use_weights: bool,
        category_cols: List[str],
    ) -> List[pl.DataFrame]:
        """
        Calculates ranking metrics (counts per rank, percentages, scores) for a ranking question
        for each category independently.
        Returns a list of DataFrames, where each inner DataFrame has rows as ranked items and columns for metrics,
        and includes the category value/label as the first column.
        """

        ranking_category_dfs: List[pl.DataFrame] = []

        possible_values = []
        if value_labels_info:
            nan_values_config = self.database.config.NAN_VALUES
            nan_values_list = []
            if isinstance(nan_values_config, (set, dict)):
                nan_values_list = list(
                    nan_values_config.keys()
                    if isinstance(nan_values_config, dict)
                    else nan_values_config
                )

            possible_values = [
                float(val)
                for val in value_labels_info.keys()
                if val not in nan_values_list
            ]

            if not possible_values:
                print(
                    f"Warning: No non-NaN possible values found for ranking question '{base_question}'. Skipping."
                )
                return []
        else:
            print(
                f"Warning: No value labels found for ranking question '{base_question}'. Cannot calculate ranking metrics. Skipping."
            )
            return []

        ranking_cols_present = [col for col in columns if col in df_group_calc.columns]

        if not ranking_cols_present:
            print(
                f"Warning: None of the ranking columns {columns} found in DataFrame for base question '{base_question}'. Skipping."
            )
            return []

        for category_col in category_cols:
            if category_col not in df_group_calc.columns:
                print(
                    f"Warning: Category column '{category_col}' not found in DataFrame for base question '{base_question}'. Skipping this category."
                )
                continue

            df_category_filtered = df_group_calc.filter(
                pl.col(category_col).is_not_null()
            )

            if df_category_filtered.is_empty():
                print(
                    f"Warning: Filtered DataFrame for category '{category_col}' is empty. Skipping."
                )
                continue

            if use_weights:
                total_respondents_count = df_category_filtered.select(
                    pl.sum(self.database.config.WEIGHT_COLUMN)
                ).item()
            else:
                total_respondents_count = df_category_filtered.shape[0]

            if total_respondents_count == 0:
                print(
                    f"Warning: Total respondents count is zero for ranking question '{base_question}' in category '{category_col}'. Skipping calculations for this category."
                )
                continue

            id_vars_melt = [category_col] + (
                [self.database.config.WEIGHT_COLUMN] if use_weights else []
            )
            id_vars_melt = [
                col for col in id_vars_melt if col in df_category_filtered.columns
            ]

            melted_df = df_category_filtered.melt(
                id_vars=id_vars_melt,
                value_vars=ranking_cols_present,
                variable_name="rank_column",
                value_name="ranked_item_value",
            )

            rank_prefix = base_question + "M"
            rank_prefix_len = len(rank_prefix)

            melted_df = melted_df.with_columns(
                [
                    pl.col("rank_column")
                    .str.slice(rank_prefix_len)
                    .cast(pl.Int64)
                    .alias("rank"),
                    pl.col("ranked_item_value")
                    .cast(pl.Int64)
                    .alias("ranked_item_value"),
                ]
            )

            melted_df = melted_df.filter(
                pl.col("rank").is_not_null() & (pl.col("rank") > 0)
            )
            melted_df = melted_df.filter(
                ~pl.col("ranked_item_value").is_in([val for val in nan_values_list])
            )
            melted_df = melted_df.filter(
                pl.col("ranked_item_value").is_in(possible_values)
            )

            if melted_df.is_empty():
                print(
                    f"Warning: Melted and filtered DataFrame for category '{category_col}' is empty after filtering. Skipping calculations for this category."
                )
                continue

            melted_df = melted_df.with_columns(
                (pl.lit(1.0) / pl.col("rank")).alias("rank_score")
            )
            if use_weights:
                melted_df = melted_df.with_columns(
                    (
                        pl.col("rank_score")
                        * pl.col(self.database.config.WEIGHT_COLUMN)
                    ).alias("weighted_rank_score")
                )

            group_cols_agg = ["ranked_item_value"]

            agg_exprs = [
                pl.count().alias("total_rank_count"),
                (
                    pl.sum("weighted_rank_score")
                    if use_weights
                    else pl.sum("rank_score")
                ).alias("total_score"),
            ]

            max_rank = len(ranking_cols_present)
            for rank_value in range(1, max_rank + 1):
                agg_exprs.append(
                    (pl.col("rank") == rank_value)
                    .sum()
                    .cast(pl.Int64)
                    .alias(f"rank_{rank_value}_count")
                )
                if use_weights:
                    agg_exprs.append(
                        pl.when(pl.col("rank") == rank_value)
                        .then(pl.col(self.database.config.WEIGHT_COLUMN))
                        .sum()
                        .cast(pl.Int64)
                        .alias(f"rank_{rank_value}_weighted")
                    )

            aggregated_ranking_df = melted_df.group_by(group_cols_agg).agg(agg_exprs)

            percentage_calc_exprs = []

            total_denominator = total_respondents_count

            if total_denominator > 0:
                for rank_value in range(1, max_rank + 1):
                    count_col_name = f"rank_{rank_value}_count"
                    percentage_col_name = f"rank_{rank_value}_percentage"
                    weighted_col_name = f"rank_{rank_value}_weighted"

                    if use_weights:
                        if weighted_col_name in aggregated_ranking_df.columns:
                            percentage_calc_exprs.append(
                                (pl.col(weighted_col_name) / pl.lit(total_denominator))
                                .fill_null(0)
                                .alias(percentage_col_name)
                            )
                        else:
                            percentage_calc_exprs.append(
                                pl.lit(0.0).alias(percentage_col_name)
                            )
                    else:
                        if count_col_name in aggregated_ranking_df.columns:
                            percentage_calc_exprs.append(
                                (pl.col(count_col_name) / pl.lit(total_denominator))
                                .fill_null(0)
                                .alias(percentage_col_name)
                            )
                        else:
                            percentage_calc_exprs.append(
                                pl.lit(0.0).alias(percentage_col_name)
                            )
            else:
                print(
                    f"Warning: Total denominator is zero for category '{category_col}'. Percentage calculations skipped."
                )
                for rank_value in range(1, max_rank + 1):
                    percentage_col_name = f"rank_{rank_value}_percentage"
                    percentage_calc_exprs.append(pl.lit(0.0).alias(percentage_col_name))

            if percentage_calc_exprs:
                aggregated_ranking_df = aggregated_ranking_df.with_columns(
                    percentage_calc_exprs
                )

            selected_cols = [pl.col("ranked_item_value").alias("Ranked Item")]

            count_cols = [
                pl.col(f"rank_{rank_value}_count").alias(f"Rank {rank_value} Count")
                for rank_value in range(1, max_rank + 1)
                if f"rank_{rank_value}_count" in aggregated_ranking_df.columns
            ]
            missing_count_cols = [
                pl.lit(0.0).alias(f"Rank {rank_value} Count")
                for rank_value in range(1, max_rank + 1)
                if f"rank_{rank_value}_count" not in aggregated_ranking_df.columns
            ]
            selected_cols.extend(count_cols + missing_count_cols)

            percentage_cols = [
                pl.col(f"rank_{rank_value}_percentage").alias(
                    f"Rank {rank_value} Percentage"
                )
                for rank_value in range(1, max_rank + 1)
                if f"rank_{rank_value}_percentage" in aggregated_ranking_df.columns
            ]
            missing_percentage_cols = [
                pl.lit(0.0).alias(f"Rank {rank_value} Percentage")
                for rank_value in range(1, max_rank + 1)
                if f"rank_{rank_value}_percentage" not in aggregated_ranking_df.columns
            ]
            selected_cols.extend(percentage_cols + missing_percentage_cols)

            if use_weights:
                weighted_cols = [
                    pl.col(f"rank_{rank_value}_weighted").alias(
                        f"Rank {rank_value} Weighted Sum"
                    )
                    for rank_value in range(1, max_rank + 1)
                    if f"rank_{rank_value}_weighted" in aggregated_ranking_df.columns
                ]
                missing_weighted_cols = [
                    pl.lit(0.0).alias(f"Rank {rank_value} Weighted Sum")
                    for rank_value in range(1, max_rank + 1)
                    if f"rank_{rank_value}_weighted"
                    not in aggregated_ranking_df.columns
                ]
                selected_cols.extend(weighted_cols + missing_weighted_cols)

            if "total_score" in aggregated_ranking_df.columns:
                selected_cols.append(pl.col("total_score").alias("Total Score"))
            else:
                selected_cols.append(pl.lit(0.0).alias("Total Score"))

            selected_cols.append(
                pl.lit(total_respondents_count).alias("Total Respondents")
            )

            per_category_df = aggregated_ranking_df.select(selected_cols).sort(
                "Ranked Item"
            )

            per_category_df = per_category_df.with_columns(
                pl.lit(category_col).alias("Category")
            ).select(["Category"] + per_category_df.columns)

            ranking_category_dfs.append(per_category_df)

        return ranking_category_dfs

    def index(self, weights=False, scale=None, correlate=None):
        """
        Calculates index scores using vectorized Polars operations.
        Handles overall index, category-based index, and optional scaling.
        Stores the result in self.database.index.
        """
        print("\n--- Start calculating index ---")

        df_clean = self.database.df.clone()
        use_weights = weights and (self.database.config.WEIGHT_COLUMN is not None)
        if use_weights:
            weight_column = self.database.config.WEIGHT_COLUMN

        nan_values_config = self.database.config.NAN_VALUES
        nan_values_list = []
        if isinstance(nan_values_config, (set, dict)):
            nan_values_list = list(
                nan_values_config.keys()
                if isinstance(nan_values_config, dict)
                else nan_values_config
            )

        if nan_values_list:
            flag_expressions = [
                pl.col(col).is_in(nan_values_list).alias(f"{col}_was_nan_value_code")
                for col in df_clean.columns
                if col in self.database.meta.variable_value_labels
                and any(
                    value in nan_values_list
                    for value in self.database.meta.variable_value_labels[col].keys()
                )
            ]
            if flag_expressions:
                df_clean = df_clean.with_columns(flag_expressions)

            replace_expressions = [
                pl.col(col).replace(nan_values_config)
                for col in df_clean.columns
                if col in self.database.meta.variable_value_labels
                and any(
                    value in nan_values_list
                    for value in self.database.meta.variable_value_labels[col].keys()
                )
            ]
            if replace_expressions:
                df_clean = df_clean.with_columns(replace_expressions)

        all_questions = [
            q for qlist in self.database.config.area_map.values() for q in qlist
        ]
        questions_present = [q for q in all_questions if q in df_clean.columns]

        if correlate:
            correlate_df = self._correlate(df_clean, correlate, questions_present)

        if not questions_present:
            print(
                "Warning: No questions from area_map found in DataFrame. Cannot calculate index."
            )
            self.database.index = pl.DataFrame()
            print("\n--- calculations done ---")
            return self.database.index

        if self.database.categories.is_empty():
            print("Calculating overall index.")
            df_long = df_clean.select(
                [weight_column] + questions_present if weights else questions_present
            ).melt(
                id_vars=[weight_column] if weights else [],
                value_vars=questions_present,
                variable_name="Question",
                value_name="Value",
            )

            df_long = df_long.drop_nulls(subset=["Value"])

            if df_long.is_empty():
                print(
                    "Warning: No valid data after dropping nulls for overall index calculation."
                )
                self.database.index = pl.DataFrame()
                print("\n--- calculations done ---")
                return self.database.index

            if weights and weight_column in df_long.columns:
                overall_index_expr = (
                    pl.col("Value").fill_null(1) * pl.col(weight_column).fill_null(0)
                ).sum() / pl.col(weight_column).fill_null(1).sum()
            else:
                overall_index_expr = pl.col("Value").mean()

            overall_index_df = (
                df_long.select(overall_index_expr.alias("Index"))
                .with_columns(pl.lit("Overall").alias("Category"))
                .select(["Category", "Index"])
            )

            self.database.index = overall_index_df.with_columns(
                pl.col("Index").round(5)
            )
            print("Overall index calculation complete.")

            print("\n--- calculations done ---")
            return self.database.index

        print("Calculating category-based index.")
        categories = self.database.categories

        results_list = []

        area_map_list = []
        for area_name, questions in self.database.config.area_map.items():
            for question in questions:
                area_map_list.append({"Question": question, "Frågeområde": area_name})

        area_map_df = pl.DataFrame(area_map_list)

        for category_column in categories:
            if category_column not in df_clean.columns:
                print(
                    f"Warning: Category column '{category_column}' not found in DataFrame. Skipping index calculation for this category."
                )
                continue

            category_membership_value = 1
            df_category_filtered = (
                df_clean.filter(pl.col(category_column) == category_membership_value)
                .with_columns(pl.lit(category_column).alias("Category"))
                .drop(category_column)
            )
            nan_flag_cols = [
                col
                for col in df_category_filtered.columns
                if col.endswith("_was_nan_value_code")
                and df_category_filtered.schema[col] == pl.Boolean
            ]
            if not nan_flag_cols:
                nan_counts = pl.DataFrame(
                    {
                        "Question": pl.Series([], dtype=pl.Utf8),
                        "Nan_Count": pl.Series([], dtype=pl.Float64),
                    }
                )
            else:
                nan_boolean_df = df_category_filtered.select(nan_flag_cols)
                nan_counts = nan_boolean_df.sum().transpose(
                    include_header=True,
                    header_name="Question",
                    column_names=["Nan_Count"],
                )
                nan_counts = nan_counts.with_columns(
                    pl.col("Question").str.replace("_was_nan_value_code$", "")
                )

            if df_category_filtered.is_empty():
                print(
                    f"Warning: Filtered DataFrame for category '{category_column}' is empty. Skipping index calculation for this category."
                )
                continue

            if weights:
                select_columns = ["Category", weight_column] + questions_present
            else:
                select_columns = ["Category"] + questions_present
            melted_df = df_category_filtered.select(select_columns).melt(
                id_vars=["Category", weight_column] if weights else ["Category"],
                value_vars=questions_present,
                variable_name="Question",
                value_name="Value",
            )
            melted_df = melted_df.join(nan_counts, on="Question", how="left")

            melted_df = melted_df.drop_nans(subset=["Value"])

            if melted_df.is_empty():
                print(
                    f"Warning: Melted DataFrame for category '{category_column}' is empty after dropping nulls. Skipping index calculation for this category."
                )
                continue

            melted_df = melted_df.join(area_map_df, on="Question", how="left")

            melted_df = melted_df.filter(pl.col("Frågeområde").is_not_null())

            if melted_df.is_empty():
                print(
                    f"Warning: Melted DataFrame for category '{category_column}' is empty after assigning and filtering Frågeområde. Skipping index calculation for this category."
                )
                continue

            if scale and len(scale) == 2:
                question_meta_ranges = []
                relevant_questions_df = self.database.question_df.filter(
                    pl.col("question").is_in(questions_present)
                )
                for row in relevant_questions_df.iter_rows(named=True):
                    q_name = row["question"]
                    value_labels_info = row["value_labels_info"]

                    if value_labels_info and isinstance(value_labels_info, dict):
                        numeric_values = []
                        for key in value_labels_info.keys():
                            try:
                                value = float(key)
                                if value not in self.database.config.NAN_VALUES:
                                    numeric_values.append(value)
                            except (ValueError, TypeError):
                                continue

                        if numeric_values:
                            question_meta_ranges.append(
                                {
                                    "Question": q_name,
                                    "meta_original_min": min(numeric_values),
                                    "meta_original_max": max(numeric_values),
                                }
                            )
                        else:
                            print(
                                f"Warning: No numeric value labels found in value_labels_info for question '{q_name}'. Skipping its scaling metadata."
                            )
                    else:
                        print(
                            f"Warning: No valid value_labels_info (or not a dict) found for question '{q_name}'. Skipping its scaling metadata."
                        )

                question_meta_ranges_df = pl.DataFrame(
                    question_meta_ranges,
                    schema={
                        "Question": pl.Utf8,
                        "meta_original_min": pl.Float64,
                        "meta_original_max": pl.Float64,
                    },
                )
                melted_df = melted_df.join(
                    question_meta_ranges_df, on="Question", how="left"
                )
                if "meta_original_min" not in melted_df.columns:
                    print(
                        "Error: Could not join metadata ranges for scaling. Skipping scaling."
                    )
                else:
                    target_min, target_max = scale
                    melted_df = (
                        melted_df.with_columns(
                            pl.when(
                                pl.col("meta_original_max")
                                > pl.col("meta_original_min")
                            )
                            .then(
                                (pl.col("Value") - pl.col("meta_original_min"))
                                * (
                                    (target_max - target_min)
                                    / (
                                        pl.col("meta_original_max")
                                        - pl.col("meta_original_min")
                                    )
                                )
                                + target_min
                            )
                            .otherwise(pl.col("Value"))
                            .alias("Value_scaled")
                        )
                        .drop(["meta_original_min", "meta_original_max", "Value"])
                        .rename({"Value_scaled": "Value"})
                    )
                    melted_df = melted_df.with_columns(pl.col("Value").cast(pl.Float64))

            if weights and weight_column in melted_df.columns:
                individual_index_expr = (
                    (pl.col("Value") * pl.col(weight_column)).sum()
                    / pl.col(weight_column).sum()
                ).alias("Individual_Index")
            else:
                individual_index_expr = pl.mean("Value").alias("Individual_Index")

            individual_question_index_df = (
                melted_df.fill_null(0)
                .group_by(["Category", "Question", "Frågeområde", "Nan_Count"])
                .agg(
                    [
                        individual_index_expr,
                        pl.count("Value").alias("Count_Individual"),
                    ]
                )
                .with_columns(
                    pl.when(
                        (pl.col("Count_Individual") + pl.col("Nan_Count"))
                        < self.database.config.MINIMUM_COUNT
                    )
                    .then(pl.lit(None))
                    .otherwise(pl.col("Individual_Index"))
                    .alias("Individual_Index")
                    .cast(pl.Float64)
                )
                .drop("Count_Individual")
                .drop("Nan_Count")
            )
            melted_df = melted_df.join(
                individual_question_index_df,
                on=["Category", "Question", "Frågeområde"],
                how="left",
            )

            if weights and weight_column in melted_df.columns:
                area_index_expr = (
                    pl.col("Value") * pl.col(weight_column)
                ).sum() / pl.col(weight_column).sum()
            else:
                area_index_expr = pl.col("Value").mean()

            area_index_df = (
                melted_df.group_by(["Category", "Frågeområde"])
                .agg(
                    [
                        area_index_expr.alias("Area_Index"),
                    ]
                )
                .with_columns(pl.col("Area_Index").alias("Area_Index").cast(pl.Float64))
            )

            final_category_df = individual_question_index_df.join(
                area_index_df, on=["Category", "Frågeområde"], how="left"
            )

            selected_cols_for_category_df = [
                pl.col("Category"),
                pl.col("Frågeområde"),
                pl.col("Question"),
                pl.col("Individual_Index"),
                pl.col("Area_Index"),
            ]

            final_category_df = final_category_df.select(selected_cols_for_category_df)

            results_list.append(final_category_df)

        if results_list:
            final_result = pl.concat(results_list, how="vertical")

            individual_index_pivot = final_result.pivot(
                index="Category",
                columns=["Frågeområde", "Question"],
                values="Individual_Index",
                aggregate_function="first",
            )

            area_index_long = final_result.select(
                ["Category", "Frågeområde", "Area_Index"]
            ).melt(
                id_vars=["Category", "Frågeområde"],
                value_name="Area_Index_Value",
            )

            area_index_pivot = area_index_long.pivot(
                index="Category",
                columns="Frågeområde",
                values="Area_Index_Value",
                aggregate_function="first",
            )

            final_result_wide = individual_index_pivot.join(
                area_index_pivot, on="Category", how="left"
            )

            ordered_columns = ["Category"]
            existing_columns = final_result_wide.columns

            for area_name, questions in self.database.config.area_map.items():
                for q in questions:
                    col_string_representation = f'{{"{area_name}","{q}"}}'
                    if col_string_representation in existing_columns:
                        ordered_columns.append(col_string_representation)
                    else:
                        print(
                            f"Warning: Individual question column '{col_string_representation}' not found in final wide result. Skipping column ordering."
                        )

                area_index_col_name = area_name
                if area_index_col_name in existing_columns:
                    ordered_columns.append(area_index_col_name)
                else:
                    print(
                        f"Warning: Area index column '{area_index_col_name}' not found in final wide result. Skipping column ordering for this area index."
                    )

            final_ordered_cols_present = [
                col for col in ordered_columns if col in existing_columns
            ]
            final_result_ordered = final_result_wide.select(final_ordered_cols_present)
            rename_mapping = {}
            for area_name, questions in self.database.config.area_map.items():
                for q in questions:
                    old_col_name = f'{{"{area_name}","{q}"}}'
                    if old_col_name in final_result_ordered.columns:
                        rename_mapping[old_col_name] = q

            if rename_mapping:
                final_result_ordered = final_result_ordered.rename(rename_mapping)

            self.database.index_df = final_result_ordered
            print("Category-based index calculation complete.")

    def _correlate(
        self, df: pl.DataFrame, correlate_area: str, questions: List[str]
    ) -> pl.DataFrame:
        """
        Calculate correlation for each category using Polars.
        Correlates the average score of questions within the specified 'correlate_area'
        against each individual question within that same area, for each category.
        Handles NaN values by replacing specific codes with Polars nulls before calculation.

        Args:
            df: The cleaned DataFrame (self.database.df after NaN replacement).
            correlate_area: The name of the area (key in area_map) to correlate within.
            questions: A list of all questions present in the DataFrame from area_map.

        Returns:
            A Polars DataFrame with correlation results for each category and question within the area.
        """
        print(f"\n--- Calculating correlation within area '{correlate_area}' ---")

        correlation_results_list = []

        if correlate_area not in self.database.config.area_map:
            print(
                f"Error: Correlate area '{correlate_area}' not found in area_map. Cannot calculate correlation."
            )
            self.database.correlation_df = pl.DataFrame(
                {"Category": [], "Area": [], "Question": [], "Correlation": []}
            )
            return self.database.correlation_df

        correlate_area_questions = self.database.config.area_map[correlate_area]
        if not correlate_area_questions:
            print(
                f"Warning: No questions defined for correlate area '{correlate_area}'. Cannot calculate correlation."
            )
            self.database.correlation_df = pl.DataFrame(
                {"Category": [], "Area": [], "Question": [], "Correlation": []}
            )
            return self.database.correlation_df

        category_cols = self.database.categories
        category_cols_present = [col for col in category_cols if col in df.columns]

        if not category_cols_present:
            print(
                "Warning: No category columns found in DataFrame. Calculating overall correlation for the area."
            )
            if len(questions) < 2:
                print(
                    f"Warning: Need at least two numeric questions in area '{correlate_area}' for overall correlation. Skipping."
                )
                self.database.correlation_df = pl.DataFrame(
                    {"Category": [], "Area": [], "Question": [], "Correlation": []}
                )
                return self.database.correlation_df

            df_overall = df.select(questions)
            df_overall = df_overall.with_columns(
                df_overall.select(correlate_area_questions)
                .mean_horizontal()
                .alias(f"{correlate_area}_avg")
            )
            avg_col_name = correlate_area

            overall_corr_list = []
            for question in questions:
                try:
                    df_subset = df_overall.select(
                        [pl.col(avg_col_name), pl.col(question)]
                    ).drop_nans()

                    if df_subset.height > 1:
                        correlation_value = df_subset.corr(method="pearson")

                        if (
                            correlation_value is not None
                            and not correlation_value.is_empty()
                            and correlation_value.height > 1
                            and correlation_value.width > 1
                        ):
                            corr_val = correlation_value[question][
                                correlation_value.find_idx_by_name(avg_col_name)
                            ].item()

                            overall_corr_list.append(
                                pl.DataFrame(
                                    {
                                        "Category": ["Overall"],
                                        "Area": [correlate_area],
                                        "Question": [question],
                                        "Correlation": [corr_val],
                                    }
                                )
                            )
                        else:
                            print(
                                f"Warning: Correlation calculation returned unexpected result for overall correlation, question '{question}'. Skipping."
                            )
                    else:
                        print(
                            f"Warning: Not enough data points for overall correlation, question '{question}'. Skipping."
                        )

                except pl.exceptions.ComputeError as e:
                    print(
                        f"Error calculating overall correlation for question '{question}': {e}. Skipping."
                    )
                    continue
                except Exception as e:
                    print(
                        f"An unexpected error occurred during overall correlation calculation for question '{question}': {e}. Skipping."
                    )
                    continue

            final_correlation_df = (
                pl.concat(overall_corr_list, how="vertical")
                if overall_corr_list
                else pl.DataFrame(
                    {"Category": [], "Area": [], "Question": [], "Correlation": []}
                )
            )

            self.database.correlation_df = final_correlation_df.with_columns(
                pl.col("Correlation").round(5)
            )
            print("Overall correlation calculations complete.")
            return self.database.correlation_df

        for category_col in category_cols_present:
            print(f"Calculating correlation for category: {category_col}")

            category_membership_value = 1
            filtered_df = (
                df.filter(pl.col(category_col) == category_membership_value)
                .with_columns(pl.lit(category_col).alias("Category"))
                .drop(category_col)
            )

            if filtered_df.is_empty():
                print(
                    f"Warning: Filtered DataFrame for category '{category_col}' is empty. Skipping correlation calculation for this category."
                )
                continue

            df_category_area = filtered_df.select(questions)

            if df_category_area.is_empty():
                print(
                    f"Warning: Area DataFrame is empty for category '{category_col}'. Skipping correlation."
                )
                continue

            if not questions:
                print(
                    f"Warning: No numeric questions found for area '{correlate_area}' in category '{category_col}'. Skipping correlation."
                )
                continue

            try:
                df_category_area = df_category_area.with_columns(
                    df_category_area.select(correlate_area_questions)
                    .mean_horizontal()
                    .alias(correlate_area)
                )
                avg_col_name = correlate_area
            except pl.exceptions.ComputeError as e:
                print(
                    f"Error calculating average for area '{correlate_area}' in category '{category_col}': {e}. Skipping correlation for this category."
                )
                continue
            except Exception as e:
                print(
                    f"An unexpected error occurred during average calculation for area '{correlate_area}' in category '{category_col}': {e}. Skipping correlation for this category."
                )
                continue

            for question in questions:
                try:
                    df_subset = df_category_area.select(
                        [pl.col(avg_col_name), pl.col(question)]
                    ).drop_nans()
                    if df_subset.height > 1:
                        correlation_value = df_subset.select(
                            pl.corr(question, avg_col_name)
                        )

                        if correlation_value is not None:
                            corr = correlation_value[question]
                            if corr.is_empty():
                                corr_val = 0.0
                            else:
                                corr_val = correlation_value[question].item()
                                if not corr_val >= 0:
                                    corr_val = 0.0

                            correlation_results_list.append(
                                pl.DataFrame(
                                    {
                                        "Category": category_col,
                                        "Area": correlate_area,
                                        "Question": question,
                                        "Correlation": corr_val,
                                    }
                                )
                            )

                        else:
                            print(
                                f"Warning: Correlation calculation returned unexpected result for category '{category_col}', question '{question}'. Skipping."
                            )
                    else:
                        print(
                            f"Warning: Not enough data points for correlation in category '{category_col}', question '{question}'. Skipping."
                        )

                except pl.exceptions.ComputeError as e:
                    print(
                        f"Error calculating correlation for category '{category_col}', question '{question}': {e}. Skipping."
                    )
                    continue
                except Exception as e:
                    print(
                        f"An unexpected error occurred during correlation calculation for category '{category_col}', question '{question}': {e}. Skipping."
                    )
                    continue

        final_correlation_df = (
            pl.concat(correlation_results_list, how="diagonal")
            if correlation_results_list
            else pl.DataFrame(
                {"Category": [], "Area": [], "Question": [], "Correlation": []}
            )
        )

        self.database.correlate_df = final_correlation_df
        print("Correlation calculations complete.")
        return self.database.correlate_df

    def eni(self, area: str, weights=False):
        """
        Calculates index scores using vectorized Polars operations.
        Handles overall index, category-based index, and optional scaling.
        Stores the result in self.database.index.
        """
        print("\n--- Start calculating ENI ---")

        df_clean = self.database.df.clone()
        use_weights = weights and (self.database.config.WEIGHT_COLUMN is not None)
        if use_weights:
            weight_column = self.database.config.WEIGHT_COLUMN

        nan_values_config = self.database.config.NAN_VALUES
        nan_values_list = []
        if isinstance(nan_values_config, (set, dict)):
            nan_values_list = list(
                nan_values_config.keys()
                if isinstance(nan_values_config, dict)
                else nan_values_config
            )

        if nan_values_list:
            flag_expressions = [
                pl.col(col).is_in(nan_values_list).alias(f"{col}_was_nan_value_code")
                for col in df_clean.columns
                if col in self.database.meta.variable_value_labels
                and any(
                    value in nan_values_list
                    for value in self.database.meta.variable_value_labels[col].keys()
                )
            ]
            if flag_expressions:
                df_clean = df_clean.with_columns(flag_expressions)

            replace_expressions = [
                pl.col(col).replace(nan_values_config)
                for col in df_clean.columns
                if col in self.database.meta.variable_value_labels
                and any(
                    value in nan_values_list
                    for value in self.database.meta.variable_value_labels[col].keys()
                )
            ]
            if replace_expressions:
                df_clean = df_clean.with_columns(replace_expressions)

        questions_present = [
            q for q in self.database.config.area_map.get(area) if q in df_clean.columns
        ]

        if not questions_present:
            print(
                "Warning: No questions from area_map found in DataFrame. Cannot calculate ENI."
            )
            self.database.index = pl.DataFrame()
            print("\n--- calculations done ---")
            return self.database.index

        print("Calculating category-based ENI.")
        categories = self.database.categories

        results_list = []

        for category_column in categories:
            if category_column not in df_clean.columns:
                print(
                    f"Warning: Category column '{category_column}' not found in DataFrame. Skipping index calculation for this category."
                )
                continue

            category_membership_value = 1
            df_category_filtered = (
                df_clean.filter(pl.col(category_column) == category_membership_value)
                .with_columns(pl.lit(category_column).alias("Category"))
                .drop(category_column)
            )
            nan_flag_cols = [
                col
                for col in df_category_filtered.columns
                if col.endswith("_was_nan_value_code")
                and df_category_filtered.schema[col] == pl.Boolean
            ]
            if not nan_flag_cols:
                nan_counts = pl.DataFrame(
                    {
                        "Question": pl.Series([], dtype=pl.Utf8),
                        "Nan_Count": pl.Series([], dtype=pl.Float64),
                    }
                )
            else:
                nan_boolean_df = df_category_filtered.select(nan_flag_cols)
                nan_counts = nan_boolean_df.sum().transpose(
                    include_header=True,
                    header_name="Question",
                    column_names=["Nan_Count"],
                )
                nan_counts = nan_counts.with_columns(
                    pl.col("Question").str.replace("_was_nan_value_code$", "")
                )

            if df_category_filtered.is_empty():
                print(
                    f"Warning: Filtered DataFrame for category '{category_column}' is empty. Skipping ENI calculation for this category."
                )
                continue

            if weights:
                select_columns = ["Category", weight_column] + questions_present
            else:
                select_columns = ["Category"] + questions_present
            df_category = df_category_filtered.select(select_columns).with_columns(
                pl.mean_horizontal(questions_present).alias("Value")
            )

            eni_df = df_category.with_columns(
                pl.when(pl.col("Value").is_between(3.0, 4.19))
                .then(pl.lit("2"))
                .when(pl.col("Value") >= 4.2)
                .then(pl.lit("3"))
                .otherwise(pl.lit("1"))
                .alias("ENI_Category")
            )
            eni_counts_df = eni_df.group_by("Category", "ENI_Category").agg(
                pl.count().alias("category_count")
            )
            total_counts_df = eni_df.group_by("Category").agg(
                pl.count().alias("total_responses")
            )
            eni_proportions_df = (
                eni_counts_df.join(total_counts_df, on=["Category"], how="left")
                .with_columns(
                    (pl.col("category_count") / pl.col("total_responses")).alias(
                        "ENI_Proportion"
                    )
                )
                .sort("Category", "ENI_Category")
            )

            results_list.append(eni_proportions_df)

        if results_list:
            final_result = pl.concat(results_list, how="vertical")

            pivot = final_result.pivot(
                index="Category",
                columns=["ENI_Category"],
                values="ENI_Proportion",
                aggregate_function="first",
            ).fill_null(0)
            self.database.eni_df = pivot
            print("Category-based ENI calculation complete.")
            percentages = self._eni_percentage()
            return self.database.eni_df

    def _eni_percentage(self):
        df = self.database.percentage_df

        grouped_answer_expr = (
            pl.when(pl.col("answer_value").is_in(["1.0", "2.0"]))
            .then(pl.lit("1-2"))
            .when(pl.col("answer_value").is_in(["3.0", "4.0"]))
            .then(pl.lit("3-4"))
            .when(pl.col("answer_value") == "5.0")
            .then(pl.lit("5"))
            .otherwise(pl.lit("Other"))
            .alias("grouped_answer_value")
        )
        agg_expressions = [pl.sum(col).alias(col) for col in self.database.categories]
        recoded_df = (
            df.group_by("question", grouped_answer_expr, "metric_type")
            .agg(*agg_expressions)
            .sort("question", "grouped_answer_value")
        )
        final_df = recoded_df.with_columns(
            pl.when(pl.col("grouped_answer_value") == "1-2")
            .then(pl.lit("Motarbeidere"))
            .when(pl.col("grouped_answer_value") == "3-4")
            .then(pl.lit("Nøytrale"))
            .when(pl.col("grouped_answer_value") == "5")
            .then(pl.lit("Engasjerte"))
            .otherwise(pl.lit("Ukjent"))
            .alias("label")
        )
        self.database.eni_percentage_df = final_df
        return final_df

    def open_text(self) -> pl.DataFrame:
        """
        Extracts open text responses from the main DataFrame and stores them
        in a Polars DataFrame (self.database.open_text_df).

        Returns:
            pl.DataFrame: A DataFrame containing 'base_question' and 'response' columns
                          for all extracted open text data.
        """
        print("\n--- Extracting Open Text Responses ---")

        main_df = self.database.df

        open_text_questions_meta = self.database.question_df.filter(
            pl.col("question_type") == "open_text"
        )

        all_open_text_responses_list = []

        for row in open_text_questions_meta.iter_rows(named=True):
            base_question = row["base_question"]
            question_columns = row.get("question", [])

            if not question_columns:
                print(
                    f"Warning: No columns defined for open text question '{base_question}'. Skipping."
                )
                continue

            responses_series = (
                main_df.select(
                    pl.col(base_question).cast(pl.Utf8).alias("response_temp")
                )
                .filter(
                    pl.col("response_temp").is_not_null()
                    & (pl.col("response_temp").str.strip_chars() != "")
                )
                .get_column("response_temp")
            )

            if responses_series.len() > 0:
                temp_df = pl.DataFrame(
                    {
                        "base_question": [base_question] * responses_series.len(),
                        "response": responses_series,
                    },
                    schema={"base_question": pl.Utf8, "response": pl.Utf8},
                )
                all_open_text_responses_list.append(temp_df)
            else:
                print(
                    f"No valid responses found for open text question '{base_question}'."
                )

        if all_open_text_responses_list:
            self.database.open_text_df = pl.concat(
                all_open_text_responses_list, how="vertical"
            )
            print(
                f"Extracted {self.database.open_text_df.shape[0]} open text responses."
            )
        else:
            self.database.open_text_df = pl.DataFrame(
                {"base_question": [], "response": []},
                schema={"base_question": pl.Utf8, "response": pl.Utf8},
            )
            print("No open text responses found or extracted.")

        print("\n--- Open Text Extraction Complete ---")
        return self.database.open_text_df
