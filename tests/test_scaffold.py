def test_application_package_imports() -> None:
    import llm_app

    assert llm_app.__doc__ == "LLM application package."
