import asyncio
from configure.logger import setup_logger, log_info

from agent.blog_agent import BlogWritterAgent
main_agent = BlogWritterAgent()



# -------------------------------------------
# Setup logging
# -------------------------------------------
logger = setup_logger(name="blog_writter", tag="MAIN")



# -------------------------------------------
# Usage
# -------------------------------------------
if __name__ == "__main__":
    
    try:
        result = asyncio.run(main_agent())
        
    except Exception as e:
        log_info(logger,f"Error running blog research: {e}")