import dataiku
def write_output_dataset(input_name, output_name, df, source_webapp=None, description=None):
    dataiku.TABLES[output_name] = df; return {"connection": "test"}
